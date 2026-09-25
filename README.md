# Herdr Command Runner

Experimental foreground command capture for local and SSH execution. A Herdr
pane shows live output; raw output and confirmed exit status come from process
pipes rather than terminal scrollback. Version **0.1.0**.

Requires Python 3.8+ locally, Python 3.6+ on an SSH host, and macOS or Linux.
Herdr 0.9.1+ is optional for execution and required for the plugin viewer.
No daemon, queue, third-party Python package, or remote Herdr install is required.

## Install locally

```sh
herdr plugin link /absolute/path/to/herdr-command-runner
export PATH="/absolute/path/to/herdr-command-runner/bin:$PATH"
herdr-command doctor
```

The PATH export applies to the current shell; add it to your shell configuration
if you want it to persist. Linking does not modify shell configuration.

## Run a command

```sh
herdr-command exec --cwd /absolute/path/to/project -- python3 -m unittest
herdr-command exec --ssh build-host --cwd /srv/project -- make test
herdr-command exec --ssh build-host --cwd /srv/project --context build-tree -- make all
```

Use a configured SSH alias; normal SSH authentication and host verification apply.
Establish authentication and trust interactively beforehand. Automation uses
BatchMode and never accepts a new host key itself. Each command requires an
explicit working directory. Arguments after `--` are literal argv values;
pipelines and redirection require an explicit interpreter such as `sh -c`.
Execution is general purpose and can modify files: review the command you run.

From a Herdr pane, add `--view` before `--` to open a dedicated output tab without
stealing focus. Outside Herdr, supply `--workspace` with an explicitly chosen
local workspace ID. Execution continues even if the viewer cannot open.
You can also display a run independently:

```sh
herdr-command view /path/to/run-directory
```

The CLI prints the result file immediately. It records `stdout.bin`, `stderr.bin`,
`transport.log`, and `result.json` in a private run directory outside the repo,
by default `~/.local/state/herdr-command/runs`. Override its parent with
`--output-dir`. The viewer sanitizes terminal controls; the binary files preserve
exact bytes. stdout/stderr ordering across streams is not guaranteed.

The result contains command arguments, execution context, timestamps, artifact
paths, and the actual child exit status (including signal termination). The CLI
returns 0 for success, 1 for failure/rejection, 75 for busy, 125 for unknown outcome,
and 2 for invalid usage. Read `exit_code` for the actual command exit status.

## Reliability boundaries

- Foreground, non-interactive commands only: stdin is closed and there is no PTY.
  Programs may buffer output when not attached to a terminal; use their own
  unbuffered option when needed. Interactive password prompts are unsupported.
- No overall timeout by default. `--timeout SECONDS` is optional. Silence does
  not imply failure. SSH has a 15-second connection-establishment timeout.
- Timeout, interruption, broken SSH, or malformed protocol preserve captured
  output and report **unknown** unless completion was received and validated.
  Never automatically rerun a command whose outcome is unknown.
- Closing the viewer does not stop the runner. Closing/killing the runner can
  interrupt its transport; remote termination is not guaranteed. SIGKILL or power
  loss can leave a stale `running` record: it is not proof the command still runs.
- Per-host file locks reject simultaneous commands in the same context. Default
  context is the canonical working directory; `--context` groups related work.
  Locks coordinate this tool, not unrelated programs. Commands that daemonize or
  close inherited descriptors can escape its lifecycle and are unsupported.
- Login shell output written to SSH stdout corrupts the protocol and fails
  visibly. Fix noisy non-interactive startup scripts rather than trusting partial
  captures. SSH diagnostics are kept in `transport.log`.
- Raw artifacts and command arguments can contain secrets; keep them private.
  Disk usage grows with output. Disk-write failures produce an unknown outcome.
  No telemetry or uploads are performed.

## Test and extend

```sh
python3 -m unittest discover -s tests -v
python3 tests/stress_long.py
```

Tests use synthetic commands and a fake SSH transport through real local shells.
They require no product-specific tools or private network access. The separate
long test takes just over five minutes. See [adapter contract](docs/adapters.md)
and [validation](docs/validation.md).

## Uninstall and publication

```sh
herdr plugin unlink community.command-runner
```

Remove your PATH entry separately. Unlinking preserves source and private run
artifacts. Delete those only when no command is active and you no longer need them.

This repository has no publishing remote or selected license yet. Before a public
release, choose an appropriate license and review the tracked files/history.
Do not add private adapters, configuration, captured output, or credentials.
The plugin is independent of any product-specific toolkit.
