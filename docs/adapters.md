# Optional context adapters

The generic runner works without adapters. An adapter is explicitly trusted
Python source selected by `exec --adapter FILE --adapter-config PRIVATE.json`.
It runs as the execution user and must be reviewed like any executable code.
The source and selected JSON configuration travel over the existing SSH channel;
they are not installed remotely. Do not transmit unnecessary secrets.

An adapter implements two functions:

```python
def enter(config, bootstrap):
    # Return argv for a context-entering tool that executes bootstrap.
    # bootstrap is an already-encoded Python invocation, suitable for a
    # Bash/tcsh command argument. Run it in the foreground without rewriting it.
    return ["context-tool", "enter", config["name"], "--exec", bootstrap]

def prepare(config, argv, cwd):
    # Runs inside the selected context, under the execution lock.
    # Check identity and prerequisites; raise if uncertain.
    # Return argv, absolute working directory, JSON-serializable provenance.
    return argv, cwd, {"context_verified": True}
```

Neither Python hook may write to stdout. Validation errors become rejected
results. The context-entry program may print startup diagnostics; these go to
transport.log while a private temporary FIFO carries the inner endpoint's binary
stream. The FIFO is foreground IPC and is removed on normal exit. It is not a
background worker or a durable job. `enter` must remain in the foreground until
the inner endpoint exits. It must not rewrite the bootstrap or start detached jobs.
`prepare` must finish verification before returning the command, avoid source
mutations, and use bounded capture for its own diagnostics.

Use a stable `--context` identity for mutually exclusive work even if different
directories refer to it. No plugin registry or implicit adapter discovery is used.
Private context adapters can live in separate repositories; the public runner
does not import them or depend on their configuration formats.
