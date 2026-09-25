import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr_command.cli import Protocol
from herdr_command.machine import Machine

ROOT = Path(__file__).resolve().parents[1]


class PaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HERDR_COMMAND_STATE_DIR=str(self.root / "state"))
        self.identity = dict(machine="machine-1", target="test-host", session="default", pane_id="w1:p1", terminal_id="t1", shell_pid=os.getpid())
        self.request = dict(nonce="a"*32, argv=[sys.executable, "-c", "print('inherited output')"],
                            cwd=str(self.root), context="prepared", pane_identity=self.identity, startup_timeout=2,
                            worker_source=(ROOT / "herdr_command/worker.py").read_text(),
                            pane_source=(ROOT / "herdr_command/pane_endpoint.py").read_text())

    def collect(self, request=None, prepare=True):
        proc = subprocess.Popen([sys.executable, str(ROOT / "herdr_command/pane_endpoint.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        proc.stdin.write(json.dumps(request or self.request).encode()+b"\n")
        proc.stdin.close()
        proc.stdin = None
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        header = proc.stdout.read(5)
        self.assertEqual(header[:1], b"J")
        data = proc.stdout.read(struct.unpack("!I", header[1:])[0])
        item = json.loads(data)
        if prepare:
            self.assertEqual(item["event"], "prepared")
        return proc, item

    def capture(self, prepared):
        directory = prepared["remote_directory"]
        code = "import runpy,sys;runpy.run_path(sys.argv[1]+'/endpoint.py',init_globals={'CAPTURE_DIR':sys.argv[1]},run_name='__main__')"
        process = subprocess.Popen([sys.executable, "-c", code, directory], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def results(self, proc, nonce=None):
        raw, errors = proc.communicate(timeout=10)
        outputs, controls = [], []
        parser = Protocol(nonce or self.request["nonce"], lambda kind,data: outputs.append((kind,data)), controls.append)
        for offset in range(0, len(raw), 7919):
            parser.feed(raw[offset:offset+7919])
        return outputs, controls, errors

    def test_runs_in_inherited_context_without_reentry(self):
        self.request["adapter_source"] = "def enter(*args): raise RuntimeError('must never enter')\ndef prepare(c,a,d): return a,d,{'prepared':True}\n"
        self.request["adapter_config"] = {}
        relay, ready = self.collect()
        capture = self.capture(ready)
        capture.communicate(timeout=10)
        output, events, _ = self.results(relay)
        self.assertEqual(b"".join(x[1] for x in output), b"inherited output\n")
        self.assertTrue(next(e for e in events if e["event"] == "started")["provenance"]["prepared"])
        self.assertEqual(events[-1]["exit_code"], 0)

    def test_busy_reservation_rejects_second_submission(self):
        relay, ready = self.collect()
        other = dict(self.request, nonce="b"*32)
        second, state = self.collect(other, prepare=False)
        self.assertEqual(state["status"], "busy")
        second.communicate(timeout=5)
        process = self.capture(ready)
        process.communicate(timeout=5)
        self.results(relay)

    def test_expired_request_cannot_execute_later(self):
        marker = self.root / "should-not-exist"
        self.request.update(startup_timeout=.1, argv=[sys.executable,"-c","from pathlib import Path;Path("+repr(str(marker))+").touch()"])
        relay, ready = self.collect()
        time.sleep(.2)
        process = self.capture(ready)
        _, error = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"expired", error)
        relay.communicate(timeout=5)
        self.assertFalse(marker.exists())

    def test_capture_checks_actual_parent_shell(self):
        self.request["pane_identity"] = dict(self.identity, shell_pid=999999)
        relay, ready = self.collect()
        process = self.capture(ready)
        _, error = process.communicate(timeout=5)
        self.assertIn(b"registered foreground shell", error)
        relay.communicate(timeout=5)

    def test_disconnect_keeps_output_and_recovery_does_not_rerun(self):
        marker = self.root / "executions"
        self.request["argv"] = [sys.executable, "-c", "import time;from pathlib import Path;p=Path("+repr(str(marker))+");p.write_text(p.read_text()+'x' if p.exists() else 'x');print('before',flush=True);time.sleep(.3);print('after')"]
        relay, ready = self.collect()
        process = self.capture(ready)
        time.sleep(.1)
        relay.terminate()
        relay.communicate(timeout=5)
        process.communicate(timeout=5)
        request = dict(self.request, recover=True)
        recovery = subprocess.Popen([sys.executable, str(ROOT / "herdr_command/pane_endpoint.py")], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        recovery.stdin.write(json.dumps(request).encode()+b"\n")
        recovery.stdin.close(); recovery.stdin = None
        output, events, _ = self.results(recovery)
        self.assertEqual(b"".join(x[1] for x in output), b"before\nafter\n")
        self.assertEqual(events[-1]["exit_code"], 0)
        self.assertEqual(marker.read_text(), "x")

    def test_duplicate_capture_is_rejected(self):
        relay, ready = self.collect()
        self.capture(ready).communicate(timeout=5)
        second = self.capture(ready)
        _, error = second.communicate(timeout=5)
        self.assertIn(b"already claimed", error)
        self.results(relay)

    def test_machine_pins_terminal_and_rejects_busy_pane(self):
        pane = dict(pane_id="w1:p1", terminal_id="t1")
        foreground = dict(pid=123, name="tcsh")
        def call(instance, *args, **kwargs):
            if args[0] == "machine":
                return [dict(id="machine-1", label="test", target="test-host", session="default", enabled=True)]
            if args[1] == "list": return {"panes":[pane]}
            if args[1] == "process-info": return {"process_info":dict(pane_id="w1:p1",foreground_process_group_id=123,foreground_processes=[foreground])}
            raise AssertionError("No command should be submitted")
        with patch.object(Machine,"call",call):
            selected = Machine("test", "w1:p1")
            pane["terminal_id"] = "replacement"
            with self.assertRaises(ValueError): selected.launch("never")
            foreground["name"] = "vim"
            with self.assertRaises(ValueError): Machine("test", "w1:p1")

    def test_successful_empty_pane_run_response_is_accepted(self):
        machine = object.__new__(Machine)
        machine.binary = "herdr"
        machine.selector = "test"
        with patch("herdr_command.machine.subprocess.run", return_value=subprocess.CompletedProcess([],0,b"",b"")):
            self.assertEqual(machine.call("pane","run","w1:p1","command"),{})
            with self.assertRaises(ValueError): machine.call("pane","list")

    def test_saved_binding_defers_checks_but_never_skips_live_validation(self):
        calls=[]
        pane=dict(pane_id='w1:p1',terminal_id='t1')
        process=dict(pane_id='w1:p1',foreground_process_group_id=123,
                     foreground_processes=[dict(pid=123,name='tcsh')])
        binding=dict(self.identity,terminal_id='t1',shell_pid=123)
        def call(instance,*args,**kwargs):
            calls.append(args)
            if args[0]=='machine':return [dict(id='machine-1',label='test',target='test-host',session='default',enabled=True)]
            if args[1]=='list':return {'panes':[pane]}
            if args[1]=='process-info':return {'process_info':process}
            if args[1]=='run':return {}
            raise AssertionError(args)
        with patch.object(Machine,'call',call):
            machine=Machine('test','w1:p1',binding=binding,defer_inspection=True)
            self.assertEqual(calls,[('machine','list','--json')])
            machine.launch('approved command')
            self.assertEqual(sum(c[:2]==('pane','list') for c in calls),1)
            self.assertEqual(sum(c[:2]==('pane','process-info') for c in calls),1)
            for changed in ('terminal','busy','wrong-pane'):
                calls.clear()
                pane['terminal_id']='other' if changed=='terminal' else 't1'
                process['foreground_processes'][0]['name']='vim' if changed=='busy' else 'tcsh'
                process['pane_id']='other' if changed=='wrong-pane' else 'w1:p1'
                with self.assertRaises(ValueError):machine.launch('must not submit')
                self.assertFalse(any(c[:2]==('pane','run') for c in calls))
            for field,value in (('target','different-host'),('session','different-session'),('shell_pid',None),('terminal_id','')):
                with self.assertRaises(ValueError):
                    Machine('test','w1:p1',binding=dict(binding,**{field:value}),defer_inspection=True)

    def test_pane_and_process_checks_run_concurrently(self):
        barrier=threading.Barrier(2)
        def call(instance,*args,**kwargs):
            if args[0]=='machine':return [dict(id='machine-1',label='test',target='test-host',session='default',enabled=True)]
            barrier.wait(timeout=2)
            if args[1]=='list':return {'panes':[dict(pane_id='w1:p1',terminal_id='t1')]}
            return {'process_info':dict(pane_id='w1:p1',foreground_process_group_id=123,
                                        foreground_processes=[dict(pid=123,name='tcsh')])}
        with patch.object(Machine,'call',call):
            self.assertEqual(Machine('test','w1:p1').identity['shell_pid'],123)

    def test_request_deadline_must_be_finite(self):
        for option in ("--startup-timeout", "--timeout"):
            for value in ("nan", "inf", "-1"):
                result = subprocess.run([sys.executable, str(ROOT / "bin/herdr-command"), "exec", "--cwd", str(self.root),
                                         option+"="+value, "--", sys.executable, "-c", "print('must not execute')"], capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"must not execute", result.stdout)


if __name__ == "__main__": unittest.main()
