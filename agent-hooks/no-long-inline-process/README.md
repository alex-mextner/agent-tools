# no-long-inline-process

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 35

A long-running process started inline by the **orchestrator** blocks the main thread for
minutes. This gate **hard-blocks** it (this is the most clear-cut case, so straight block,
not warn-first) and tells the orchestrator to dispatch it to a **background subagent**.
Enforces the "stay responsive" half of `delegate-work-to-subagents`.

Blocked (conservative, anchored at a command start so a substring in a path/word never trips):

- the **`review`** CLI invoked as a command (multi-model, minutes-long)
- any **`--watch`** flag (`gh pr checks --watch`, `vitest --watch`, `tsc --watch`, …)
- a **build/test suite**: `npm|pnpm|yarn|bun|deno test|build`, `pytest`/`vitest`/`jest`/
  `cypress`/`playwright`, `cargo test|build`, `go test|build`, `make|rake|msbuild test|build|all`,
  `mvn|gradle test|build|verify|package`
- **`sleep N`** with `N >= 10` (short sleeps like `sleep 2` are fine)

Leading **no-op wrappers** are peeled off each segment before matching, so a wrapped long
process is still caught — `timeout 600 npm test`, `env CI=1 pytest`, `timeout 5m review`,
`nice -n10 review`, `time make build`, `stdbuf -oL pytest`, `nohup cargo build`. The wrapper's
own args (`timeout`'s duration, `env`'s `KEY=VALUE` assignments, `-k`/`-n`/`--signal` flags) are
skipped; only the *wrapped* command's long-running-ness decides (`timeout 5 ls` stays allowed).

**Subagent-exempt:** a dispatched subagent (`agent_id` present) is *expected* to run these in
the background, so it is always allowed. This gate governs the orchestrator only. `args.agent_id`
must originate from a **trusted, transport-level** signal — never from model-controlled
`tool_input`. `cc_hook_bridge` forwards it only from CC's top-level event and drops any
`tool_input`-forged copy; a non-CC carrier must replicate that filtering or a forged `agent_id`
self-exempts the orchestrator (full contract in `background-subagent-gate/README.md`).

## Why a hard block (not warn-first)

The other delegation gates warn-then-block, but a long inline process is unambiguous: it WILL
stall the main thread, every time. There is no borderline case to warn about — so this one
blocks on the first occurrence, with a real escape hatch for the rare deliberate exception.

## Escape hatch (controllable, not a hard wall)

```bash
# session-wide override (reason REQUIRED, or it still blocks):
ALLOW_INLINE_PROCESS=1 ALLOW_INLINE_PROCESS_REASON="one-shot smoke build, output needed now"

# one-off, self-documenting:
npm test   # inline-process-ok: single fast unit file, not the full suite
```

A reasonless `ALLOW_INLINE_PROCESS=1` is ignored and the command stays blocked.

## Fail-open, on purpose

`on_error: "open"`. Responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x no_long_inline_process.py
echo '{"args":{"command":"review"}}'   | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=10 (block)
echo '{"args":{"command":"npm test"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=10 (block)
echo '{"args":{"command":"sleep 2"}}'  | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=0 (allow)
echo '{"args":{"command":"cat docs/review-notes.md"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"  # → exit=0 (path, not a command)
echo '{"args":{"agent_id":"sub-1","command":"npm test"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"  # → exit=0 (subagent-exempt)
```
