# enforce-timeout-on-bash

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Detects hangable shell commands — `curl`/`wget`, package installs, builds, test runners
(`jest`/`vitest`/`pytest`/`playwright`), `docker`/`kubectl`, browser/Electron launches —
that are run **without** a `timeout` wrapper (or an equivalent tool-level timeout flag),
and:

- **default (advisory):** warns via stderr and allows, surfacing a reminder to bound it;
- **strict mode** (`ENFORCE_TIMEOUT_STRICT=1`): **blocks**, so the agent re-issues the
  command with a bound.

It skips commands that are already bounded (`timeout 60 …`, `--max-time`, `--timeout`) and
instant read-only ones (`--version`, `--help`).

## Why an agent-hook

The bound has to be added *to the command being issued*, before it runs — exactly when a
`pre-bash` hook fires. Nothing downstream can retroactively bound a command that already
hung. Enforces the `shell-timeouts` skill.

## Fail-open

`on_error: "open"`, and advisory by default — a timeout discipline reminder should never
itself wedge the session. Flip to strict only where you want a hard guarantee that no
unbounded hangable command ever runs.

## Test

```bash
chmod +x enforce_timeout.py
echo '{"args":{"command":"npm test"}}' | ./enforce_timeout.py 2>&1; echo "exit=$?"
# default: warns on stderr, decision allow, exit=0
ENFORCE_TIMEOUT_STRICT=1 sh -c 'echo "{\"args\":{\"command\":\"npm test\"}}" | ./enforce_timeout.py'; echo "exit=$?"
# strict: decision block, exit=10

echo '{"args":{"command":"timeout 600 npm test"}}' | ./enforce_timeout.py; echo "exit=$?"
# → allow, exit=0 (already bounded)
```
