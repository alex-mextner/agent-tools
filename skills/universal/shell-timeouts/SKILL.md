---
name: shell-timeouts
description: Use whenever you run a shell command that can hang — builds, test runs, network fetches, installs, browser/Electron automation, or anything hitting an external service. Wrap it in a timeout so a stuck process can't leave orphans.
---

# Set a timeout on every hangable shell command

Any command that touches the network, spawns a browser, runs a build, or waits on
an external service can hang forever. When it does, the calling agent or CI job
either blocks indefinitely or drops the call and leaves an orphan process behind.
The fix is cheap: put an explicit upper bound on every such command.

## Rule

Wrap the command in shell `timeout N` **and/or** pass an explicit timeout to your
tool runner. Don't rely on a tool's default — defaults are silent and easy to drop.

- Short read-only command (<5s expected): a small explicit timeout is enough.
- Medium (build one file, run one test file): `timeout 60 …`.
- Long (full build, full test suite, container build, deploy): `timeout 600 …`
  and run it detached/in background.

## Exceptions

A few commands legitimately run for many minutes and must NOT be capped short —
multi-round AI panels, long syntheses, big migrations. For those, set a *large*
timeout (or none) rather than a small one. "Always cap" is about commands that
*hang*; it is not about commands that *work for a long time*.

## Example

```bash
# Network fetch — bound it.
timeout 30 curl -fsSL https://example.com/data.json -o data.json

# Test suite — bound it and capture the real exit code.
timeout 600 npm test
echo "exit=$?"

# Browser automation — bound the whole run, and clean up your own children
# before retrying after a failed run.
timeout 180 node ./e2e/run.js || true
pkill -9 -f "my-isolated-userdata-prefix" 2>/dev/null || true
```

Rationale: a once-in-ten hang means the *next* invocation has to kill a process
by hand. An explicit bound turns a silent hang into a fast, actionable failure.
