"""Foreground execution endpoint. Kept compatible with Python 3.6 on SSH hosts.

stdout is a length-prefixed binary protocol, never a terminal transcript.
User programs receive separate pipes and cannot emit control frames.
"""
import base64
import fcntl
import hashlib
import json
import os
import selectors
import struct
import subprocess
import sys
import tempfile
import time
import zlib


def write_all(stream, data):
    pending = memoryview(data)
    while pending:
        count = stream.write(pending)
        if not count:
            raise OSError("Output stream stopped accepting bytes")
        pending = pending[count:]


def bootstrap(source, request=None):
    encoded = base64.b64encode(zlib.compress(source.encode())).decode()
    code = "import base64,zlib;exec(zlib.decompress(base64.b64decode(\"" + encoded + "\")))"
    if request is not None:
        data = base64.b64encode(json.dumps(request).encode()).decode()
        code = "import base64,json;REQUEST=json.loads(base64.b64decode(\"" + data + "\"));" + code
    # Only base64 and constant Python syntax enter the login shell (including tcsh).
    return "python3 -c '" + code + "'"


def main(request, source):
    nonce = request["nonce"]
    out = open(request["protocol_path"], "wb", buffering=0) if request.get("protocol_path") else sys.stdout.buffer

    def frame(kind, data):
        write_all(out, kind + struct.pack("!I", len(data)) + data)
        out.flush()

    def control(**data):
        data["nonce"] = nonce
        frame(b"J", json.dumps(data).encode())

    lock = None
    try:
        adapter = {}
        if request.get("adapter_source"):
            exec(compile(request["adapter_source"], "<explicit-adapter>", "exec"), adapter)
        if adapter and not request.get("entered"):
            # Context-entry tools often print banners. A private foreground FIFO
            # keeps those diagnostics separate from the inner process protocol.
            # This is IPC, not a job queue, daemon, or terminal completion marker.
            with tempfile.TemporaryDirectory(prefix="herdr-command-") as directory:
                path = os.path.join(directory, "stream")
                os.mkfifo(path, 0o600)
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                try:
                    inner = dict(request, entered=True, protocol_path=path)
                    command = adapter["enter"](request["adapter_config"], bootstrap(source, inner))
                    wrapper = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=sys.stderr, stderr=sys.stderr)
                    selector = selectors.DefaultSelector()
                    selector.register(descriptor, selectors.EVENT_READ)
                    try:
                        while True:
                            ready = selector.select(.1)
                            if ready:
                                data = os.read(descriptor, 65536)
                                if data:
                                    write_all(out, data)
                                    out.flush()
                            elif wrapper.poll() is not None:
                                return wrapper.returncode
                    finally:
                        selector.close()
                finally:
                    os.close(descriptor)

        cwd = os.path.realpath(os.path.expanduser(request["cwd"]))
        context = request.get("context") or cwd
        directory = os.path.join(os.path.expanduser(os.environ.get("HERDR_COMMAND_STATE_DIR", "~/.local/state/herdr-command")), "locks")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        key = hashlib.sha256(context.encode()).hexdigest()
        lock = open(os.path.join(directory, key + ".lock"), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            control(event="result", status="busy", exit_code=None, error="Execution context is busy")
            return 75
        argv = request["argv"]
        provenance = {"hostname": os.uname().nodename, "cwd": cwd, "context": context}
        if adapter:
            argv, cwd, extra = adapter["prepare"](request["adapter_config"], argv, cwd)
            provenance.update(extra)
            provenance["cwd"] = cwd
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=(lock.fileno(),))
        control(event="started", pid=process.pid, provenance=provenance, argv=argv)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, b"O")
        selector.register(process.stderr, selectors.EVENT_READ, b"E")
        while selector.get_map():
            for key, _ in selector.select():
                data = os.read(key.fileobj.fileno(), 65536)
                if data:
                    frame(key.data, data)
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        rc = process.wait()
        control(event="result", status="succeeded" if rc == 0 else "failed", exit_code=rc)
        return 0
    except BrokenPipeError:
        # A lost consumer is not evidence that the user's command stopped.
        return 74
    except Exception as exc:
        control(event="result", status="rejected", exit_code=None, error=str(exc))
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    if "REQUEST" not in globals():
        REQUEST = json.loads(sys.stdin.buffer.readline(1024 * 1024))
    # Supplied separately so adapters can enter a context with the same endpoint.
    SOURCE = REQUEST.pop("worker_source", "")
    sys.exit(main(REQUEST, SOURCE))
