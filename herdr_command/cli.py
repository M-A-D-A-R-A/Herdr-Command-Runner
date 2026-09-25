import argparse
import codecs
import datetime
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid

from . import __version__
from .worker import bootstrap, write_all

PLUGIN_ID = "community.command-runner"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class SafeText:
    """Streaming sanitizer: suppress ESC/CSI/OSC sequences, including split ones."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"

    def feed(self, data):
        output = []
        for char in self.decoder.decode(data):
            if self.state == "esc":
                self.state = "csi" if char == "[" else "string" if char in "]PX^_" else "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "string":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string-esc"
            elif self.state == "string-esc":
                self.state = "text" if char == "\\" else "string"
            elif char == "\x1b":
                self.state = "esc"
            elif char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159):
                output.append(char)
        return "".join(output)


class Protocol:
    def __init__(self, nonce, on_output, on_control):
        self.nonce = nonce
        self.output = on_output
        self.control = on_control
        self.buffer = bytearray()
        self.completed = False
        self.started = False

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 5:
            kind = bytes(self.buffer[:1])
            length = struct.unpack("!I", self.buffer[1:5])[0]
            if kind not in (b"O", b"E", b"J") or length > 262144 or self.completed:
                raise ValueError("Invalid execution stream (possibly login-shell output)")
            if len(self.buffer) < length + 5:
                break
            payload = bytes(self.buffer[5:5 + length])
            del self.buffer[:5 + length]
            if kind == b"J":
                item = json.loads(payload)
                if item.get("nonce") != self.nonce:
                    raise ValueError("Execution stream identity mismatch")
                event = item.get("event")
                if event == "started" and not self.started:
                    self.started = True
                elif event == "result":
                    if item.get("exit_code") is not None and not self.started:
                        raise ValueError("Completion without process start")
                    self.completed = True
                else:
                    raise ValueError("Invalid execution event")
                self.control(item)
            else:
                if not self.started:
                    raise ValueError("Output before process start")
                self.output(kind, payload)


def open_viewer(directory, workspace=None):
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not binary or not workspace:
        return "Viewer needs Herdr and an explicit --workspace (or a Herdr pane)."
    command = [binary, "plugin", "pane", "open", "--plugin", PLUGIN_ID,
               "--entrypoint", "output", "--placement", "tab", "--no-focus",
               "--workspace", workspace, "--env", "HERDR_COMMAND_RUN=" + str(directory)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if result.returncode:
            return SafeText().feed(result.stderr)[:1000]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc) + "; viewer launch outcome unknown; not retried"
    return None


def execute(args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise ValueError("Provide a program and arguments after --")
    if args.ssh and (args.ssh.startswith("-") or any(c.isspace() for c in args.ssh)):
        raise ValueError("Invalid SSH target")
    if not args.cwd.startswith(("/", "~")):
        raise ValueError("--cwd must be absolute or home-relative")
    if args.timeout is not None and args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    os.umask(0o077)
    root = Path(args.output_dir or Path.home() / ".local/state/herdr-command/runs").expanduser().resolve()
    directory = root / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True, mode=0o700)
    nonce = uuid.uuid4().hex
    source = Path(__file__).with_name("worker.py").read_text()
    request = dict(nonce=nonce, argv=argv, cwd=args.cwd, context=args.context, worker_source=source)
    if args.adapter:
        request["adapter_source"] = Path(args.adapter).read_text()
        request["adapter_config"] = json.loads(Path(args.adapter_config).read_text()) if args.adapter_config else {}
    encoded_request = json.dumps(request).encode() + b"\n"
    if len(encoded_request) > 1024 * 1024:
        raise ValueError("Execution request exceeds 1 MiB")
    command = (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T", args.ssh, bootstrap(source)]
               if args.ssh else [sys.executable, str(Path(__file__).with_name("worker.py"))])
    result = dict(version=__version__, run_id=directory.name, status="starting", started_at=now(),
                  argv=argv, target=args.ssh or "local", cwd=args.cwd, context=args.context,
                  exit_code=None, artifacts={name: str(directory / name) for name in
                  ("stdout.bin", "stderr.bin", "transport.log", "result.json")})
    if args.adapter:
        result["adapter"] = {"name": Path(args.adapter).name,
                             "sha256": hashlib.sha256(request["adapter_source"].encode()).hexdigest()}
    save(directory / "result.json", result)
    print(str(directory / "result.json"), flush=True)
    if args.view:
        error = open_viewer(directory, args.workspace)
        if error:
            result["viewer_error"] = error
    started = time.monotonic()
    process = None
    selector = selectors.DefaultSelector()
    final_received = False
    try:
        with open(directory / "stdout.bin", "wb", buffering=0) as stdout, \
             open(directory / "stderr.bin", "wb", buffering=0) as stderr, \
             open(directory / "transport.log", "wb", buffering=0) as transport:
            def control(item):
                nonlocal final_received
                if item["event"] == "started":
                    result.update(status="running", provenance=item["provenance"], pid=item["pid"], executed_argv=item["argv"])
                else:
                    final_received = True
                    result.update({key: value for key, value in item.items() if key not in ("nonce", "event")})
                save(directory / "result.json", result)

            protocol = Protocol(nonce, lambda kind, data: write_all(stdout if kind == b"O" else stderr, data), control)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            # Use nonblocking stdin as well: a stalled SSH login must not block timeout handling.
            os.set_blocking(process.stdin.fileno(), False)
            pending = memoryview(encoded_request)
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            selector.register(process.stdout, selectors.EVENT_READ, "protocol")
            selector.register(process.stderr, selectors.EVENT_READ, "transport")
            while selector.get_map():
                if args.timeout and time.monotonic() - started > args.timeout:
                    raise TimeoutError("Requested timeout elapsed; remote termination is not confirmed")
                for key, _ in selector.select(0.2):
                    if key.data == "input":
                        count = os.write(key.fileobj.fileno(), pending[:65536])
                        pending = pending[count:]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    elif key.data == "protocol":
                        try:
                            protocol.feed(data)
                        except (ValueError, KeyError, TypeError):
                            transport.write(b"\n[invalid execution stream chunk]\n" + data)
                            raise
                    else:
                        transport.write(data)
            while process.poll() is None:
                if args.timeout and time.monotonic() - started > args.timeout:
                    raise TimeoutError("Requested timeout elapsed while transport was closing")
                time.sleep(.05)
            result["transport_exit_code"] = process.returncode
            if protocol.buffer or not final_received:
                raise RuntimeError("Execution stream ended without a complete result; inspect transport.log")
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc) or "Interrupted; termination not confirmed"
        if final_received and isinstance(exc, (TimeoutError, KeyboardInterrupt)):
            result["transport_warning"] = message
        else:
            result.update(status="unknown", exit_code=None, error=message)
    finally:
        if process and process.poll() is None:
            # Kill only our local foreground transport/process group, never a remote server.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
        selector.close()
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        result.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
        save(directory / "result.json", result)
    print("{} (exit {})".format(result["status"], result["exit_code"]))
    return 0 if result["status"] == "succeeded" else 75 if result["status"] == "busy" else 125 if result["status"] == "unknown" else 1


def viewer(path):
    if not path:
        raise ValueError("Set HERDR_COMMAND_RUN or use view <run-directory>")
    directory = Path(path).expanduser().resolve()
    offsets = {"stdout.bin": 0, "stderr.bin": 0, "transport.log": 0}
    cleaners = {name: SafeText() for name in offsets}
    last_status = None
    last_tick = 0
    while True:
        result = json.loads((directory / "result.json").read_text())
        if last_status is None:
            print(SafeText().feed(json.dumps({key: result.get(key) for key in ("argv", "target", "cwd", "context")}).encode()), flush=True)
        for name in offsets:
            path = directory / name
            if path.exists():
                with path.open("rb") as handle:
                    handle.seek(offsets[name])
                    data = handle.read(65536)
                    offsets[name] += len(data)
                if data:
                    if name != "stdout.bin":
                        print("\n[{}]".format(name), flush=True)
                    print(cleaners[name].feed(data), end="", flush=True)
        if result["status"] != last_status or time.monotonic() - last_tick >= 5:
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(result["started_at"])).total_seconds()
            print("\n[{} | {:.0f}s | exit {}]".format(result["status"], result.get("elapsed_seconds", elapsed), result["exit_code"]), flush=True)
            if result.get("provenance") and result["status"] != last_status:
                print(SafeText().feed(json.dumps(result["provenance"]).encode()), flush=True)
            last_status = result["status"]
            last_tick = time.monotonic()
        drained = all(not (directory / name).exists() or offsets[name] >= (directory / name).stat().st_size for name in offsets)
        if result.get("finished_at") and drained:
            if result.get("error"):
                print(SafeText().feed(result["error"].encode()))
            if sys.stdin.isatty():
                input("Finished. Press Enter to close this viewer. ")
            return 0
        time.sleep(0.02 if not drained else 0.2)


def main():
    parser = argparse.ArgumentParser(description="Capture foreground commands independently of terminal scrollback.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    view = sub.add_parser("view")
    view.add_argument("directory", nargs="?", default=os.environ.get("HERDR_COMMAND_RUN"))
    run = sub.add_parser("exec")
    run.add_argument("--cwd", required=True)
    run.add_argument("--ssh", help="SSH host or configured alias; omitted means local")
    run.add_argument("--context", help="Serialize commands sharing this context on the execution host")
    run.add_argument("--output-dir", help="Parent directory for private per-run artifacts")
    run.add_argument("--timeout", type=float, help="Optional overall timeout in seconds")
    run.add_argument("--view", action="store_true")
    run.add_argument("--workspace")
    run.add_argument("--adapter", help="Explicitly trusted Python context adapter")
    run.add_argument("--adapter-config", help="Private JSON configuration for adapter")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "doctor":
            print(json.dumps({"version": __version__, "python": sys.version.split()[0],
                              "ssh": shutil.which("ssh"), "herdr": shutil.which("herdr"),
                              "platform": sys.platform, "remote": "Use exec for a target-specific probe"}, indent=2))
            return 0
        return viewer(args.directory) if args.action == "view" else execute(args)
    except (ValueError, OSError) as exc:
        print("herdr-command: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
