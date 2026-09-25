# Validation record — 0.1.0

Validated on 2026-09-25. Private artifacts are intentionally outside this repository.

| Check | Result |
|---|---|
| Synthetic runner suite | 15 tests passed on macOS |
| Login-shell quoting | Bash and tcsh; literal quotes, newlines, Unicode, shell metacharacters |
| Output integrity | 50 MiB stdout checksum matched; 100,000 stderr lines matched |
| Local long command | 306 seconds, including a 66-second quiet interval; complete capture and exit 0 |
| Real Linux SSH target | Python 3.6, tcsh; successful direct command execution |
| Real SSH stress run | 312 seconds, 52,428,800 stdout bytes, 100,000 stderr lines plus two checkpoints; checksum and exact stderr matched |
| Failure handling | Nonzero/255 exits distinguished from transport failures; partial output retained on disconnect, timeout, and interruption |
| Context adapters | Verified context, noisy startup isolation, missing executable, shared-context busy rejection |
| Viewer | Separate-process viewer closure leaves command running; terminal-control sanitizer tested |
| Herdr integration | Manifest accepted and plugin linked locally with Herdr 0.9.1 |

Actual Herdr pane rendering was subsequently verified in an isolated named
test session. To repeat from a Herdr pane, run:

```sh
herdr-command exec --cwd /tmp --view -- python3 -u -c 'import time; print("start"); time.sleep(5); print("done")'
```

Check that a dedicated tab opens without changing focus, displays elapsed time
and output, and reports success. Close it during execution and inspect the result
file to confirm capture continues. Existing investigation panes are never used
for command injection.

GitHub CI is configured for Linux and macOS but has not run remotely because the
repository has not been published. Product-specific adapter validation belongs
in the adapter's private repository. This proof does not claim recovery from
power loss, interactive/daemonizing command support, or guaranteed termination
of remote commands after a network failure.

Prepared-pane extension: nine additional tests cover inherited context without
re-entry, foreground-shell identity, changed/busy panes, single-use claims,
expired requests, concurrent submission rejection, empty successful Herdr
responses, and disconnect/recovery without re-execution. The existing capture
suite continues to pass. A live remote prepared-shell run and recovery were
verified; product-specific results remain in the private adapter repository.

Usability follow-up: 27 runner tests passed, covering stderr progress without
contaminating captured output, adapter progress, viewer command display, and
capture continuing after viewer launch failure. Real plugin panes displayed
stdout/stderr, elapsed time, and successful completion without taking focus.
Closing a real viewer during a 25-second command preserved complete output and
exit status 0. Read-only private-adapter verification also displayed its stages
and captured result in that isolated session. The temporary test server was
stopped after validation; existing user sessions were not stopped or restarted.
