import argparse
import codecs
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .worker import bootstrap, write_all
from .machine import Machine

PLUGIN_ID = "community.command-runner"


class Progress:
    """Human progress goes to stderr; stdout remains the result-path interface."""
    def __init__(self, enabled):
        self.enabled = enabled
        self.started = time.monotonic()
        self.phase = "Preparing request"
        self.stop = threading.Event()

    def report(self):
        if self.enabled:
            message = SafeText().feed(str(self.phase).encode())
            print("[command | {:.0f}s] {}".format(time.monotonic()-self.started, message), file=sys.stderr, flush=True)

    def update(self, phase):
        self.phase = phase
        self.report()

    def __enter__(self):
        def heartbeat():
            while not self.stop.wait(5):
                self.report()
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        if self.enabled:
            self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.enabled:
            self.thread.join()


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
        self.prepared = False

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
                if event == "prepared" and not self.prepared and not self.started:
                    self.prepared = True
                elif event == "progress" and not self.started and isinstance(item.get("phase"), str):
                    pass
                elif event == "started" and not self.started:
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
    with Progress(getattr(args, "progress", False)) as progress:
        return execute_with_progress(args, progress)


def execute_with_progress(args, progress):
    binding = json.loads(Path(args.binding).expanduser().read_text()) if args.binding else None
    machine = None
    if binding or args.machine or args.pane:
        progress.update("Loading the saved machine and pane binding" if binding else
                        "Checking the machine, pane, and foreground shell")
        selector = args.machine or (binding or {}).get("machine")
        pane = args.pane or (binding or {}).get("pane_id")
        if not selector or not pane:
            raise ValueError("Pane execution requires --machine and --pane, or --binding")
        machine = Machine(selector, pane, args.herdr_bin, binding, defer_inspection=bool(binding))
        if args.ssh and args.ssh != machine.profile["target"]:
            raise ValueError("SSH target must match the selected Herdr machine target")
        args.ssh = machine.profile["target"]
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise ValueError("Provide a program and arguments after --")
    if args.ssh and (args.ssh.startswith("-") or any(c.isspace() for c in args.ssh)):
        raise ValueError("Invalid SSH target")
    if not args.cwd.startswith(("/", "~")):
        raise ValueError("--cwd must be absolute or home-relative")
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
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
    if machine:
        request.update(pane_identity=machine.identity, startup_timeout=args.startup_timeout,
                       pane_source=Path(__file__).with_name("pane_endpoint.py").read_text())
    if getattr(args, "recover_request", None):
        request = dict(args.recover_request, recover=True)
        nonce = request["nonce"]
    pane_mode = "pane_source" in request
    entry_source = request["pane_source"] if pane_mode else source
    encoded_request = json.dumps(request).encode() + b"\n"
    if len(encoded_request) > 1024 * 1024:
        raise ValueError("Execution request exceeds 1 MiB")
    command = (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T", args.ssh, bootstrap(entry_source)]
               if args.ssh else [sys.executable, str(Path(__file__).with_name("worker.py"))])
    result = dict(version=__version__, run_id=directory.name, status="starting", started_at=now(),
                  argv=argv, target=args.ssh or "local", cwd=args.cwd, context=args.context,
                  label=getattr(args, "label", None),
                  exit_code=None, artifacts={name: str(directory / name) for name in
                  ("stdout.bin", "stderr.bin", "transport.log", "result.json")})
    if pane_mode:
        result.update(execution_mode="pane", pane_identity=request["pane_identity"], remote_run=nonce)
        save(directory / "request.json", request)
        result["request_path"] = str(directory / "request.json")
        if getattr(args, "recover_request", None):
            result.update(recovery_only=True, recovered_from=args.recovered_from,
                          original_started_at=args.original_started_at)
    if args.adapter:
        result["adapter"] = {"name": Path(args.adapter).name,
                             "sha256": hashlib.sha256(request["adapter_source"].encode()).hexdigest()}
    save(directory / "result.json", result)
    print(str(directory / "result.json"), flush=True)
    if args.view:
        error = open_viewer(directory, args.workspace)
        if error:
            result["viewer_error"] = error
            print("Viewer unavailable: " + error, file=sys.stderr, flush=True)
            save(directory / "result.json", result)
    progress.update("Connecting and staging the request" if args.ssh else "Starting local capture")
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
                if item["event"] == "prepared":
                    if machine is None or getattr(args, "recover_request", None):
                        raise ValueError("Unexpected pane launch request")
                    result.update(status="submitting", remote_directory=item["remote_directory"])
                    progress.update("Verifying the live pane and shell, then submitting capture")
                    save(directory / "result.json", result)
                    machine.launch(item["command"])
                    progress.update("Waiting for the capture helper to verify context")
                elif item["event"] == "progress":
                    result.update(phase=item["phase"])
                    progress.update(item["phase"])
                elif item["event"] == "started":
                    result.update(status="running", provenance=item["provenance"], pid=item["pid"], executed_argv=item["argv"])
                    result["phase"] = "Running command and capturing output"
                    progress.update(result["phase"])
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
    progress.update("{} (exit {})".format(result["status"], result["exit_code"]))
    return 0 if result["status"] == "succeeded" else 75 if result["status"] == "busy" else 125 if result["status"] == "unknown" else 1


def viewer(path):
    if not path:
        raise ValueError("Set HERDR_COMMAND_RUN or use view <run-directory>")
    directory = Path(path).expanduser().resolve()
    offsets = {"stdout.bin": 0, "stderr.bin": 0, "transport.log": 0}
    cleaners = {name: SafeText() for name in offsets}
    last_status = None
    last_tick = 0
    shown_command = None
    while True:
        result = json.loads((directory / "result.json").read_text())
        if last_status is None:
            header = "Command Runner\nRequested: {}\nTarget: {}\nDirectory: {}\nContext: {}\nArtifacts: {}\n".format(
                result.get("label") or shlex.join(result.get("argv", [])), result.get("target"), result.get("cwd"),
                result.get("context") or "default", directory)
            print(SafeText().feed(header.encode()), flush=True)
        if result.get("executed_argv") and shown_command != result["executed_argv"]:
            shown_command = result["executed_argv"]
            command_text = shlex.join(shown_command)
            if len(command_text) > 400:
                command_text = command_text[:400] + " ... [full argv in result.json]"
            print("\nExecuting: " + SafeText().feed(command_text.encode()), flush=True)
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
        visible_status = (result["status"], result.get("phase"))
        if visible_status != last_status or time.monotonic() - last_tick >= 5:
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(result["started_at"])).total_seconds()
            print("\n[{} | {:.0f}s | exit {}]".format(result["status"], result.get("elapsed_seconds", elapsed), result["exit_code"]), flush=True)
            if result.get("phase") and result["status"] in ("starting", "submitting", "running"):
                print(SafeText().feed(result["phase"].encode()), flush=True)
            if result.get("provenance") and visible_status != last_status:
                print(SafeText().feed(json.dumps(result["provenance"]).encode()), flush=True)
            last_status = visible_status
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
    bind = sub.add_parser("bind", help="Pin an explicitly selected prepared remote shell")
    bind.add_argument("--machine", required=True)
    bind.add_argument("--pane", required=True)
    bind.add_argument("--output", required=True)
    bind.add_argument("--herdr-bin", default=os.environ.get("HERDR_COMMAND_HERDR_BIN", "herdr"))
    bind.add_argument("--progress", action="store_true")
    recover = sub.add_parser("recover", help="Collect an existing pane run without submitting a command")
    recover.add_argument("directory")
    recover.add_argument("--output-dir")
    recover.add_argument("--timeout", type=float)
    recover.add_argument("--view", action="store_true")
    recover.add_argument("--workspace")
    recover.add_argument("--progress", action="store_true")
    view = sub.add_parser("view")
    view.add_argument("directory", nargs="?", default=os.environ.get("HERDR_COMMAND_RUN"))
    run = sub.add_parser("exec")
    run.add_argument("--cwd", required=True)
    run.add_argument("--ssh", help="SSH host or configured alias; omitted means local")
    run.add_argument("--machine", help="Use a prepared pane on this saved Herdr machine")
    run.add_argument("--pane", help="Explicit remote pane ID; never the focused pane")
    run.add_argument("--binding", help="Pinned machine/pane identity saved by bind")
    run.add_argument("--herdr-bin", default=os.environ.get("HERDR_COMMAND_HERDR_BIN", "herdr"))
    run.add_argument("--startup-timeout", type=float, default=60,
                     help="Pane request expires if not started within this many seconds (default 60)")
    run.add_argument("--context", help="Serialize commands sharing this context on the execution host")
    run.add_argument("--label", help="Human-readable request label; actual executed argv is recorded separately")
    run.add_argument("--output-dir", help="Parent directory for private per-run artifacts")
    run.add_argument("--timeout", type=float, help="Optional overall timeout in seconds")
    run.add_argument("--view", action="store_true")
    run.add_argument("--progress", action="store_true", help="Print stages and elapsed time on stderr")
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
        if args.action == "bind":
            with Progress(args.progress) as progress:
                progress.update("Checking machine and prepared foreground shell")
                selected = Machine(args.machine, args.pane, args.herdr_bin)
            path = Path(args.output).expanduser().resolve()
            os.umask(0o077)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            save(path, dict(selected.identity, registered_at=now()))
            print(str(path))
            return 0
        if args.action == "recover":
            directory = Path(args.directory).expanduser()
            previous = json.loads((directory / "result.json").read_text())
            request = json.loads((directory / "request.json").read_text())
            args = argparse.Namespace(**dict(vars(args), argv=request["argv"], cwd=request["cwd"],
                context=request.get("context"), ssh=previous["target"], recover_request=request,
                recovered_from=str(directory.resolve()), original_started_at=previous.get("original_started_at", previous["started_at"]),
                binding=None, machine=None, pane=None, adapter=None, herdr_bin="herdr"))
            return execute(args)
        if args.action == "exec" and (not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0):
            raise ValueError("--startup-timeout must be positive")
        return viewer(args.directory) if args.action == "view" else execute(args)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("herdr-command: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
