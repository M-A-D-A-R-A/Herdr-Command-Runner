"""Opt-in real-time check: >5 minutes overall, >1 minute without output."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(root / "bin/herdr-command"), "exec",
        "--cwd", directory, "--output-dir", directory, "--", sys.executable, "-c",
        "import time;print('start',flush=True);time.sleep(66);print('quiet complete',flush=True);time.sleep(240);print('end',flush=True)"],
        env=dict(os.environ, HERDR_COMMAND_STATE_DIR=directory + "/state"),
        stdout=subprocess.PIPE, text=True, check=True)
    result_path = Path(proc.stdout.splitlines()[0])
    result = json.loads(result_path.read_text())
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert time.monotonic() - started > 300
    assert (result_path.parent / "stdout.bin").read_text() == "start\nquiet complete\nend\n"
    print("PASS: >300 seconds, 66-second quiet interval, complete output and confirmed exit")
