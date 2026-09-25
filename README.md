# Herdr Command Runner

Run foreground commands locally or over SSH and keep a reliable record of what
happened. Herdr Command Runner captures raw stdout, stderr, exit status, and
transport diagnostics from process pipes—not from terminal scrollback.

Version **0.1.0**. It runs on macOS and Linux with Python 3.8+ locally (Python
3.6+ is supported on an SSH host). Herdr 0.9.1+ is optional for command
execution and required to use the plugin viewer.

## What it does

- Runs local commands or commands on a configured SSH host.
- Saves exact `stdout.bin`, `stderr.bin`, `transport.log`, and `result.json`
  artifacts for each run.
- Shows live output in a Herdr tab when available, without relying on terminal
  scrollback.
- Can use a deliberately selected, prepared Herdr machine pane when a command
  must inherit an existing remote shell environment.
- Needs no daemon, queue, telemetry, or third-party Python package.

## Install

Link the plugin and make the CLI available in your current shell:

```sh
herdr plugin link /absolute/path/to/herdr-command-runner
export PATH="/absolute/path/to/herdr-command-runner/bin:$PATH"
herdr-command doctor
```

Add the `PATH` export to your shell configuration if you want it to persist.
Linking the plugin does not modify shell configuration.

## Run a command

Every run needs an explicit working directory. Arguments after `--` are passed
literally to the command.

```sh
# Run locally
herdr-command exec --cwd /absolute/path/to/project -- python3 -m unittest

# Run on an SSH host configured in ~/.ssh/config
herdr-command exec --ssh build-host --cwd /srv/project -- make test

# Group related remote work under one execution context
herdr-command exec --ssh build-host --cwd /srv/project --context build-tree -- make all
```

For shell features such as pipelines or redirection, call a shell explicitly:

```sh
herdr-command exec --cwd /absolute/path/to/project -- sh -c 'make test | tee test.log'
```

Add `--view` before `--` from a Herdr pane to open a dedicated output tab
without stealing focus. Outside Herdr, pass an explicitly chosen
`--workspace` ID instead. A command continues running if the viewer cannot be
opened. Use `--label` to give the viewer request a short name; the executed
arguments remain the source of record. You can also open a saved run directly:

```sh
herdr-command view /path/to/run-directory
```

Add `--progress` to print execution stages and elapsed time on stderr, including
five-second updates during quiet waits. Stdout retains its result-path interface
and captured stdout/stderr files contain only the command's output. The viewer
shows context-verification progress and the actual executed argv once available.

## Use a prepared Herdr pane

Use this mode when the remote command must run in a dedicated shell that already
has the required environment. Select the remote pane yourself; the runner never
chooses the focused pane or creates a replacement.

```sh
# Bind a dedicated, idle pane once
herdr-command bind --machine "Build machine" --pane w1:p1 --output /private/path/build-pane.json

# Submit work through that pane
herdr-command exec --binding /private/path/build-pane.json --cwd /srv/project -- make test
```

You can instead supply `--machine "Build machine" --pane w1:p1` directly. The
saved machine must be enabled and reachable, and the designated pane should stay
dedicated to automation while requests are active.

## Results and important behavior

The CLI immediately prints the result-file path. By default, artifacts are kept
in `~/.local/state/herdr-command/runs`; use `--output-dir` to choose another
parent directory. `result.json` records the command, context, timestamps,
artifact paths, and child exit status. Use its `exit_code` for the command's
actual status.

- Commands are foreground and non-interactive: stdin is closed and no PTY is
  provided. Password prompts and daemonizing commands are unsupported.
- `--timeout` is optional. A timeout, interruption, malformed protocol, or SSH
  disconnect preserves captured output but reports an **unknown** outcome. Do
  not automatically rerun an unknown command.
- Per-host locks prevent simultaneous runner submissions in the same context;
  they do not control unrelated programs or manual typing in a prepared pane.
- Artifacts and command arguments can contain secrets. Keep run directories
  private and remove remote pane artifacts only after execution has finished.

For SSH runs, use a configured host alias and establish authentication and host
trust interactively first. Automation runs in batch mode and never accepts a new
host key itself; SSH diagnostics are saved in `transport.log`.

If a prepared-pane SSH connection drops, the remote capture can continue. Use
`herdr-command recover /path/to/original/local/run-directory` to read the saved
capture; recovery never sends another pane command.

## Test and extend

```sh
python3 -m unittest discover -s tests -v
python3 tests/stress_long.py
```

The test suite uses synthetic commands and a fake SSH transport through real
local shells. It needs no product-specific tools or private network access. The
long test takes a little over five minutes.

For implementation details, see the [adapter contract](docs/adapters.md) and
the [validation record](docs/validation.md).

## Uninstall

```sh
herdr plugin unlink community.command-runner
```

Remove the `PATH` entry separately if you no longer need the CLI. Unlinking
keeps source files and private run artifacts intact.

Before making the project public, choose a license and review tracked files and
history. Do not publish private adapters, configuration, captured output, or
credentials.
