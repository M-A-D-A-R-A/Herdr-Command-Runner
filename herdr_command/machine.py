"""Explicit, pinned Herdr machine/pane selection. Never select the focused pane."""
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor


SHELLS = {"sh", "bash", "zsh", "csh", "tcsh", "fish", "dash", "ksh"}


class Machine:
    def __init__(self, selector, pane, binary="herdr", binding=None, defer_inspection=False):
        self.binary = binary
        self.selector = selector
        self.pane = pane
        profiles = self.call("machine", "list", "--json", remote=False)
        matches = [p for p in profiles if selector in (p["id"], p["label"]) and p.get("enabled")]
        if len(matches) != 1:
            raise ValueError("Select one enabled Herdr machine by ID or unique label")
        self.profile = matches[0]
        self.selector = self.profile["id"]
        if binding and defer_inspection:
            # Reuse only the pinned identity, never a cached assertion of liveness.
            # launch() still inspects the actual pane immediately before input.
            for key, expected in (("machine", self.selector), ("target", self.profile["target"]),
                                  ("session", self.profile["session"]), ("pane_id", self.pane)):
                if binding.get(key) != expected:
                    raise ValueError("Registered pane changed ({}); register it again".format(key))
            if (not isinstance(binding.get("terminal_id"), str) or not binding["terminal_id"] or
                    type(binding.get("shell_pid")) is not int or binding["shell_pid"] <= 0):
                raise ValueError("Invalid registered terminal or shell identity; register it again")
            self.identity = {key: binding[key] for key in
                             ("machine", "target", "session", "pane_id", "terminal_id", "shell_pid")}
            return
        self.identity = self.inspect()
        if binding:
            for key in ("machine", "target", "session", "pane_id", "terminal_id", "shell_pid"):
                if self.identity[key] != binding.get(key):
                    raise ValueError("Registered pane changed ({}); register it again".format(key))

    def call(self, *args, remote=True):
        command = [self.binary] + (["--machine", self.selector] if remote else []) + list(args)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace")[:4000] or "Herdr request failed")
        if args[:2] == ("pane", "run") and not result.stdout.strip():
            return {}
        data = json.loads(result.stdout)
        return data.get("result", data) if isinstance(data, dict) else data

    def inspect(self):
        # Independent read-only requests. Parent-shell verification in capture
        # still protects the interval between inspection and input submission.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pane_request = pool.submit(self.call, "pane", "list")
            process_request = pool.submit(self.call, "pane", "process-info", "--pane", self.pane)
            panes = pane_request.result()["panes"]
            info = process_request.result()["process_info"]
        choices = [p for p in panes if p["pane_id"] == self.pane]
        if len(choices) != 1:
            raise ValueError("Registered remote pane is missing; no replacement was created")
        pane = choices[0]
        if info.get("pane_id") != self.pane:
            raise ValueError("Process information belongs to a different pane")
        foreground = info.get("foreground_processes") or []
        if (pane.get("agent") or len(foreground) != 1 or
                Path(foreground[0]["name"].lstrip("-")).name not in SHELLS or
                foreground[0]["pid"] != info.get("foreground_process_group_id")):
            raise ValueError("Remote pane is busy or its foreground shell cannot be verified")
        return {"machine": self.profile["id"], "target": self.profile["target"],
                "session": self.profile["session"], "pane_id": self.pane,
                "terminal_id": pane["terminal_id"], "shell_pid": foreground[0]["pid"]}

    def launch(self, command):
        if self.inspect() != self.identity:
            raise ValueError("Pane identity changed before submission; command was not submitted")
        # No retry: a failed response may still mean the text was submitted.
        return self.call("pane", "run", self.pane, command)
