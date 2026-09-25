"""Python 3.6 foreground relay/capture for an already prepared terminal shell.

The SSH relay stages a one-use request, announces readiness, and tails binary
frames. Herdr launches capture in the existing pane. There is no daemon or queue.
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import struct
import sys
import time


def write_all(stream, data):
    pending = memoryview(data)
    while pending:
        count = stream.write(pending)
        if not count:
            raise OSError("Stream stopped accepting data")
        pending = pending[count:]


def save(path, data):
    temporary = path + ".tmp-" + str(os.getpid())
    with open(temporary, "w") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def read(path):
    with open(path) as handle:
        return json.load(handle)


def paths(request):
    key = request["nonce"]
    if not re.match(r"^[a-f0-9]{32}$", key):
        raise ValueError("Invalid run identifier")
    root = os.path.expanduser(os.environ.get("HERDR_COMMAND_STATE_DIR", "~/.local/state/herdr-command"))
    return root, os.path.join(root, "pane-runs", key)


def guard(root, identity):
    # All Mac clients targeting this VM session and pane share one reservation.
    key = hashlib.sha256((identity["session"] + ":" + identity["pane_id"]).encode()).hexdigest()
    directory = os.path.join(root, "pane-locks")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle = open(os.path.join(directory, key + ".lock"), "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle, os.path.join(directory, key + ".json")


def emit(request, **values):
    values["nonce"] = request["nonce"]
    data = json.dumps(values).encode()
    write_all(sys.stdout.buffer, b"J" + struct.pack("!I", len(data)) + data)
    sys.stdout.buffer.flush()


def collect(request):
    os.umask(0o077)
    root, directory = paths(request)
    if not request.get("recover"):
        identity = request["pane_identity"]
        handle, active = guard(root, identity)
        try:
            previous = read(active) if os.path.exists(active) else {}
            if previous and previous.get("state") != "complete":
                expired = previous.get("state") == "prepared" and previous["expires"] < time.time()
                if not expired:
                    emit(request, event="result", status="busy", exit_code=None,
                         error="Pane has a running or unresolved request; inspect/recover it first")
                    return
            os.makedirs(directory, mode=0o700)
            request.update(entered=True, host=os.uname().nodename, uid=os.getuid(),
                           expires=time.time() + request["startup_timeout"])
            save(os.path.join(directory, "request.json"), request)
            with open(os.path.join(directory, "worker.py"), "w") as stream:
                stream.write(request["worker_source"])
            with open(os.path.join(directory, "endpoint.py"), "w") as stream:
                stream.write(request["pane_source"])
            open(os.path.join(directory, "stream.bin"), "wb").close()
            state = {"nonce": request["nonce"], "state": "prepared", "expires": request["expires"]}
            save(active, state)
            save(os.path.join(directory, "state.json"), state)
        finally:
            handle.close()
        # Encode only a private path: the potentially long command and adapter
        # travel over SSH stdin, never through terminal paste limits.
        payload = base64.b64encode(directory.encode()).decode()
        command = ('python3 -c \'import base64,runpy;d=base64.b64decode("' + payload +
                   '").decode();runpy.run_path(d+"/endpoint.py",init_globals={"CAPTURE_DIR":d},run_name="__main__")\'')
        emit(request, event="prepared", command=command, remote_run=request["nonce"],
             remote_directory=directory)
    if not os.path.isdir(directory):
        raise RuntimeError("Remote capture does not exist")
    saved = read(os.path.join(directory, "request.json"))
    with open(os.path.join(directory, "stream.bin"), "rb") as stream:
        while True:
            data = stream.read(65536)
            if data:
                write_all(sys.stdout.buffer, data)
                sys.stdout.buffer.flush()
                continue
            state = read(os.path.join(directory, "state.json"))
            if state["state"] == "complete":
                # Recheck after observing completion to avoid racing the last write.
                data = stream.read(65536)
                if data:
                    write_all(sys.stdout.buffer, data)
                    sys.stdout.buffer.flush()
                    continue
                return
            if state["state"] == "prepared" and time.time() > saved["expires"]:
                raise RuntimeError("Pane request expired before capture started; it cannot execute later")
            if state["state"] == "running":
                try:
                    os.kill(state["pid"], 0)
                except ProcessLookupError:
                    raise RuntimeError("Capture process disappeared; outcome unknown")
            time.sleep(.1)


def capture(directory):
    import runpy
    os.umask(0o077)
    request = read(os.path.join(directory, "request.json"))
    root, expected = paths(request)
    if directory != expected or request["host"] != os.uname().nodename or request["uid"] != os.getuid():
        raise RuntimeError("Capture host or user mismatch")
    if os.getppid() != request["pane_identity"]["shell_pid"]:
        raise RuntimeError("Capture was not launched by the registered foreground shell")
    handle, active = guard(root, request["pane_identity"])
    try:
        state = read(active)
        if state["nonce"] != request["nonce"] or state["state"] != "prepared" or time.time() > state["expires"]:
            raise RuntimeError("Capture request was already claimed or expired; refusing to run")
        state.update(state="running", pid=os.getpid())
        save(active, state)
        save(os.path.join(directory, "state.json"), state)
    finally:
        handle.close()
    with open(os.path.join(directory, "stream.bin"), "ab", buffering=0) as stream:
        os.dup2(stream.fileno(), 1)
        worker = runpy.run_path(os.path.join(directory, "worker.py"))
        try:
            rc = worker["main"](request, request["worker_source"])
        except BaseException:
            # Without a final framed result the collector reports unknown.
            rc = 125
        sys.stdout.buffer.flush()
    handle, active = guard(root, request["pane_identity"])
    try:
        state.update(state="complete", helper_exit_code=rc)
        save(os.path.join(directory, "state.json"), state)
        if read(active)["nonce"] == request["nonce"]:
            save(active, state)
    finally:
        handle.close()
    return rc


if __name__ == "__main__":
    if "CAPTURE_DIR" in globals():
        sys.exit(capture(CAPTURE_DIR))
    else:
        collect(json.loads(sys.stdin.buffer.readline(1024 * 1024)))
