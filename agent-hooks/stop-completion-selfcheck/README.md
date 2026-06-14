# stop-completion-selfcheck

**Point:** `stop` · **Fail policy:** `open` · **Priority:** 50

Fires when the agent is about to **end its turn**, and injects the completion self-check:

1. Did I finish *everything* in the request — code, commits, push, deploy, docs, cleanup,
   artifacts? What did I miss?
2. Concrete follow-ups — a bug noticed, an improvement worth a ticket, dead code to remove?

It does this by **blocking the stop exactly once** (exit 10 with the prompt as the block
message), so the agent has to run the check before finishing. A per-session marker (TTL
default 30min) prevents an infinite stop→block→stop loop: the second stop for the same
session is allowed, and the marker is consumed so a later genuinely-new task re-prompts.

## Why an agent-hook

This is the one rule that *can't* be anything but a Stop hook — it has to fire at the
moment the agent decides it's done, which is a turn-lifecycle event, not a tool call or a
git action. It converts the `task-completion-selfcheck` skill from "the model might
remember to do this" into "the model is reliably prompted".

## Configuration

- `SELFCHECK_MARKER_DIR` — where per-session markers live (default
  `~/.cache/agent-tools/selfcheck`)
- `SELFCHECK_TTL_S` — marker freshness window in seconds (default `1800`)

## Fail-open

`on_error: "open"`. If anything goes wrong (can't parse the event, can't write the
marker), it **allows** the stop — the worst failure mode for a stop-blocker is trapping
the agent unable to finish, so it errs the other way.

## Test

```bash
chmod +x stop_selfcheck.py
rm -rf ~/.cache/agent-tools/selfcheck
echo '{"event_id":"sess-1"}' | ./stop_selfcheck.py; echo "exit=$?"
# → decision":"block" with the self-check prompt, exit=10  (first stop)
echo '{"event_id":"sess-1"}' | ./stop_selfcheck.py; echo "exit=$?"
# → decision":"allow", exit=0  (second stop, same session — no loop)
```
