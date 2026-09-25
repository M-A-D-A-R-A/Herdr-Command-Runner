import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr_command.cli import Protocol, SafeText, open_viewer

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/herdr-command"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HERDR_COMMAND_STATE_DIR=str(self.root / "state"))

    def command(self, code, *options):
        return [sys.executable, str(CLI), "exec", "--cwd", str(self.root),
                "--output-dir", str(self.root / "runs")] + list(options) + ["--", sys.executable, "-c", code]

    def execute(self, code, *options):
        proc = subprocess.run(self.command(code, *options), env=self.env, capture_output=True, text=True, timeout=40)
        path = Path(proc.stdout.splitlines()[0])
        return proc, json.loads(path.read_text()), path.parent

    def fake_ssh(self, mode="shell", shell="/bin/sh"):
        directory = self.root / "bin"
        directory.mkdir(exist_ok=True)
        script = directory / "ssh"
        script.write_text("#!" + sys.executable + "\nimport os,sys\n" + (
            "sys.stderr.write('authentication failed\\n');sys.exit(255)\n" if mode == "auth" else
            "os.write(1,b'O'+(9).to_bytes(4,'big')+b'broken');sys.exit(255)\n" if mode == "drop" else
            "os.execl(" + repr(shell) + "," + repr(shell) + ",'-f','-c',sys.argv[-1])\n"))
        script.chmod(0o755)
        self.env["PATH"] = str(directory) + os.pathsep + os.environ["PATH"]

    def test_binary_streams_and_nonzero_exit(self):
        proc, result, folder = self.execute("import os,sys;os.write(1,bytes(range(256))*1000);os.write(2,'héllo\\n'.encode());sys.exit(23)")
        self.assertEqual(result["exit_code"], 23)
        self.assertEqual(result["status"], "failed")
        self.assertEqual((folder / "stdout.bin").read_bytes(), bytes(range(256)) * 1000)
        self.assertEqual((folder / "stderr.bin").read_text(), "héllo\n")
        self.assertEqual(proc.returncode, 1)

    def test_exit_255_is_not_transport_failure(self):
        self.fake_ssh()
        _, result, _ = self.execute("import sys;sys.exit(255)", "--ssh", "test-host")
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 255))

    def test_shells_and_special_arguments(self):
        value = "quotes ' \" $HOME `false` $(false) ; ! \\ newline\n雪"
        for shell in ("/bin/bash", "/bin/tcsh"):
            if not Path(shell).exists():
                continue
            with self.subTest(shell=shell):
                self.fake_ssh(shell=shell)
                cmd = self.command("import json,sys;print(json.dumps(sys.argv[1:]))", "--ssh", "test-host") + [value]
                proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=15)
                result_path = Path(proc.stdout.splitlines()[0])
                self.assertEqual(proc.returncode, 0, result_path.read_text())
                self.assertEqual(json.loads((result_path.parent / "stdout.bin").read_bytes()), [value])

    def test_auth_and_dropped_connection(self):
        for mode in ("auth", "drop"):
            self.fake_ssh(mode)
            _, result, _ = self.execute("print('never')", "--ssh", "test-host")
            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["exit_code"])

    def test_timeout_retains_partial_output(self):
        _, result, folder = self.execute("import time;print('partial',flush=True);time.sleep(60)", "--timeout", "0.6")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((folder / "stdout.bin").read_text(), "partial\n")

    def test_interrupt_retains_partial_output(self):
        process = subprocess.Popen(self.command("import time;print('partial',flush=True);time.sleep(60)"),
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        path = Path(process.stdout.readline().strip())
        for _ in range(100):
            capture = path.parent / "stdout.bin"
            if capture.exists() and capture.stat().st_size:
                break
            time.sleep(.02)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=10)
        self.assertEqual(json.loads(path.read_text())["status"], "unknown")
        self.assertEqual(capture.read_text(), "partial\n")

    def test_real_disconnect_preserves_captured_output(self):
        self.fake_ssh()
        script = self.root / "bin/ssh"
        script.write_text("#!" + sys.executable + "\n" +
            "import os,struct,subprocess,sys\n"
            "p=subprocess.Popen(['/bin/sh','-c',sys.argv[-1]],stdin=sys.stdin,stdout=subprocess.PIPE)\n"
            "while True:\n"
            " h=p.stdout.read(5)\n"
            " if len(h)!=5: break\n"
            " data=p.stdout.read(struct.unpack('!I',h[1:])[0])\n"
            " os.write(1,h+data)\n"
            " if h[:1]==b'O': break\n"
            "p.stdout.close()\np.wait()\nsys.exit(255)\n")
        _, result, folder = self.execute("import time;print('before disconnect',flush=True);time.sleep(.3)", "--ssh", "test-host")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((folder / "stdout.bin").read_text(), "before disconnect\n")

    def test_missing_program_is_rejected(self):
        cmd = self.command("unused")
        cmd = cmd[:cmd.index("--") + 1] + ["/nonexistent/command"]
        proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=10)
        result = json.loads(Path(proc.stdout.splitlines()[0]).read_text())
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["exit_code"])

    def test_context_lock_and_independent_context(self):
        process = subprocess.Popen(self.command("import time;print('ready',flush=True);time.sleep(2)", "--context", "shared"),
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        path = Path(process.stdout.readline().strip())
        for _ in range(100):
            if json.loads(path.read_text())["status"] == "running":
                break
            time.sleep(.02)
        _, result, _ = self.execute("print('no')", "--context", "shared")
        self.assertEqual(result["status"], "busy")
        _, result, _ = self.execute("print('yes')", "--context", "other")
        self.assertEqual(result["status"], "succeeded")
        process.communicate(timeout=10)

    def test_capture_fifty_mib_and_lines(self):
        size = 50 * 1024 * 1024
        _, result, folder = self.execute("import os\nfor i in range(800): os.write(1,b'x'*65536)\nfor i in range(100000): os.write(2,b'line\\n')")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((folder / "stdout.bin").stat().st_size, size)
        expected = hashlib.sha256()
        for _ in range(800):
            expected.update(b"x" * 65536)
        actual = hashlib.sha256()
        with (folder / "stdout.bin").open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                actual.update(chunk)
        self.assertEqual(actual.digest(), expected.digest())
        self.assertEqual((folder / "stderr.bin").read_bytes().count(b"\n"), 100000)

    def test_viewer_closure_does_not_stop_command(self):
        process = subprocess.Popen(self.command("import time;print('start',flush=True);time.sleep(1);print('end')"),
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        path = Path(process.stdout.readline().strip())
        view = subprocess.Popen([sys.executable, str(CLI), "view", str(path.parent)], stdout=subprocess.DEVNULL)
        time.sleep(.2)
        view.terminate()
        view.wait(timeout=5)
        process.communicate(timeout=10)
        self.assertEqual(json.loads(path.read_text())["status"], "succeeded")
        self.assertEqual((path.parent / "stdout.bin").read_text(), "start\nend\n")

    def test_explicit_adapter_context(self):
        adapter = self.root / "adapter.py"
        adapter.write_text("import subprocess\ndef enter(config,bootstrap):\n return ['/bin/sh','-c',bootstrap]\ndef prepare(config,argv,cwd):\n return argv,cwd,{'verified':True}\n")
        _, result, _ = self.execute("print('adapter')", "--adapter", str(adapter))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["provenance"]["verified"])

    def test_noisy_context_entry_does_not_corrupt_protocol(self):
        adapter = self.root / "adapter.py"
        adapter.write_text("import sys\ndef enter(config,bootstrap):\n return [sys.executable,'-c',\"import subprocess,sys;print('startup banner',flush=True);sys.exit(subprocess.call(['/bin/sh','-c',sys.argv[1]]))\",bootstrap]\ndef prepare(config,argv,cwd):\n return argv,cwd,{'verified':True}\n")
        _, result, folder = self.execute("print('actual output')", "--adapter", str(adapter))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((folder / "stdout.bin").read_text(), "actual output\n")
        self.assertIn("startup banner", (folder / "transport.log").read_text())

    def test_sanitizer_split_escape_sequences(self):
        cleaner = SafeText()
        chunks = [b"safe\x1b]52;c;", b"malicious\x07", b"\x1b[31", b"mred\x1b[0m", b"\x00\r\b\xc2\x9b"]
        self.assertEqual("".join(cleaner.feed(part) for part in chunks), "safered")

    def test_protocol_bounds_and_identity(self):
        protocol = Protocol("expected", lambda *args: None, lambda *args: None)
        with self.assertRaises(ValueError):
            protocol.feed(b"O" + struct.pack("!I", 99999999))
        payload = json.dumps({"nonce": "wrong", "event": "started"}).encode()
        protocol = Protocol("expected", lambda *args: None, lambda *args: None)
        with self.assertRaises(ValueError):
            protocol.feed(b"J" + struct.pack("!I", len(payload)) + payload)


if __name__ == "__main__":
    unittest.main()
