# Laptop and VM setup

The runner and viewer live on the laptop. A prepared-pane Linux VM needs only
Herdr 0.9.1+, Python 3.6+, and working SSH access. The runner transfers its small
capture helper per request. No pip packages, Codex, repository clone, or runner
plugin installation is required on the VM.

## 1. Laptop: install the runner from this checkout

Keep this repository in a permanent directory. With Python 3.8+ and Herdr 0.9.1+
already installed, run from the repository root:

```sh
python3 bin/herdr-command-setup local
```

This links the viewer plugin and `~/.local/bin/herdr-command`, and adds the user
bin directory to your login shell PATH. Open a new terminal afterwards.
It does not install/update Herdr locally or modify SSH configuration.

## 2. Copy the small setup script to the VM

Replace `build-host` with your own existing SSH alias. First connect interactively
with `ssh build-host` to establish authentication and host trust. Then, from the
laptop repository root:

```sh
scp bin/herdr-command-setup build-host:herdr-command-setup
ssh build-host
```

Only this script is needed remotely. These instructions work with a local
checkout before a public release; they do not rely on an unpublished download URL.

## 3. VM: run setup yourself

Works from Bash, zsh, and tcsh:

```sh
python3 ~/herdr-command-setup vm
```

The script keeps a compatible existing Herdr. Otherwise it downloads the official
**0.9.1** Linux x86_64 or aarch64 executable directly from GitHub, verifies a pinned
SHA256 and the executable version, then installs it atomically in `~/.local/bin`.
It backs up a replaced executable. No sudo is required. Python and curl must
already exist; use your VM administrator's supported installation method if
Python is missing. The setup script does not install operating-system packages.

Shell startup changes are backed up and repeatable. For tcsh it retains the
existing `.cshrc` fallback when no `.tcshrc` exists; Bash login and noninteractive
startup files and zsh's `.zshenv` are handled separately. Use `--no-path` to manage
PATH yourself. No Herdr server is started or restarted by setup.

Reconnect to the VM so the new PATH takes effect, then run:

```sh
herdr --version
herdr
```

Prepare your environment in a dedicated Herdr pane and leave it idle. Installation
checks are not proof that a Herdr server, SSH machine, or prepared context is ready.
An already-running older server requires a separately planned restart; the setup
script intentionally does not stop sessions with running work.

## 4. Laptop: connect and select the prepared pane

```sh
herdr machine add build-host --label "Build machine"
herdr --machine "Build machine" pane list
herdr-command bind --machine "Build machine" --pane YOUR_PANE_ID --output ~/build-pane.json
herdr-command exec --binding ~/build-pane.json --cwd /your/project -- python3 -c 'print("capture ready")'
```

Use the actual pane ID returned by `pane list`. Inspect `result.json` and the
capture files. A private adapter may supply its own machine launcher and context
verification; follow its setup instructions instead of this generic bind command.
In particular, Herdr 0.9.1 has a tcsh SSH bootstrap issue: the private toolkit
provides a scoped compatibility launcher. This generic installer does not repair
that upstream issue or modify the login shell.

## Blocked VM downloads

The setup script prints the exact GitHub asset URL on a failed download. Download
that **0.9.1** Linux asset on another machine, copy it to the VM, and run there:

```sh
python3 ~/herdr-command-setup vm --binary ~/herdr-linux-x86_64
```

For an ARM VM use `herdr-linux-aarch64`. The same pinned checksum verification
applies. An incomplete, wrong-version, or wrong-platform file cannot replace the
existing executable. Download failures do not silently change network settings or
disable certificate checks.

For a read-only prerequisite check, use `vm --check` or `local --check`.
Neither check verifies the live server version, machine binding, or context.
Future release pins are updated in this script after verification; it is not a
rolling updater. Existing Herdr versions >=0.9.1 are preserved, not downgraded.

## Uninstall

On the laptop, run `herdr plugin unlink community.command-runner` and remove
`~/.local/bin/herdr-command` only if it is this setup's symlink. Remove the two-line
`Herdr Command Runner: user bin PATH` block from startup files if no longer needed.
Do not remove the directory from PATH if other software uses it.

On the VM, Herdr is shared software: only remove `~/.local/bin/herdr` when it is
no longer used. Retained `herdr.before-command-setup-*` files can restore the
previous executable. Setup does not delete sessions, bindings, or captured output.
