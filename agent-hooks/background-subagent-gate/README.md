# background-subagent-gate

**Point:** `pre-agent` · **Fail policy:** `open` · **Priority:** 30

Fires when the **main thread** dispatches a subagent (the CC `Agent`/`Task` tool). A
non-trivial subagent run in the **foreground** blocks the orchestrator until it finishes —
which defeats fanning work out. This gate **blocks** such a dispatch and tells the
orchestrator to set `run_in_background: true` (or model the work as a dynamic Workflow).

Lets through:

- a dispatch already marked `run_in_background: true` (the desired shape)
- a **trivial** one-liner dispatch (prompt/description `< 200` chars and single-line)
- a dispatch made **by a subagent itself** (subagent-exempt — `agent_id` present): a worker
  may fan out further, and this gate governs the orchestrator, not the workers

## Why an agent-hook (and the `pre-agent` point)

"Dispatch heavy work in the background" was advice only. An autonomous orchestrator with
auto-accepted prompts will happily fire a foreground subagent and block itself for minutes.
The only place to catch it is **before the Agent/Task call runs** — a new `pre-agent` point
the bridge now maps from CC's `PreToolUse` on `Agent`/`Task`. It enforces the orchestration
half of `delegate-work-to-subagents`.

> The `agent_id`/`agent_type` subagent signal is forwarded by `lib/cc_hook_bridge` into the
> v1 event. For CC to actually fire this point, rig-cli must wire an `Agent|Task` PreToolUse
> matcher in `settings.json` (`hook_bridge_entries`) — a **rig-cli follow-up**, separate repo.
>
> **Trust-boundary contract (any carrier, not just CC).** This hook (and `orchestrator-stays-thin`,
> `no-long-inline-process`) exempts a tool use when `args.agent_id` is present — so that value must
> originate from a **trusted, transport-level** subagent signal, never from model-controlled
> `tool_input`. `cc_hook_bridge` enforces this: it takes `agent_id`/`agent_type` only from CC's
> top-level event and **drops** any copy a prompt forged inside `tool_input`. A non-CC carrier that
> wires these hooks MUST replicate that filtering, or a forged `tool_input.agent_id` would let the
> orchestrator exempt itself from the gate.

## Triviality heuristic

A prompt is trivial (cheap to run inline) only if it is **short** (`< 200` chars) **and**
single-line. Deliberately conservative: we block only a **clearly** non-trivial foreground
dispatch, so a borderline one slipping through is preferred to nagging on quick tasks.

Triviality is judged on the **prompt/description length and shape only** — not the
`subagent_type` or model. A short prompt to a heavyweight agent still reads as trivial here;
the gate's job is "don't block the main thread on a long foreground run", and prompt size is
the cheap, payload-only proxy for that.

## Escape hatch (controllable, not a hard wall)

```bash
# session-wide override (reason REQUIRED, or it still blocks):
ALLOW_FOREGROUND_SUBAGENT=1 ALLOW_FOREGROUND_SUBAGENT_REASON="quick probe, latency matters"
```

A reasonless `ALLOW_FOREGROUND_SUBAGENT=1` is ignored and the dispatch stays blocked. There
is **no inline sentinel** — the Agent tool carries no shell string to hide a `# ...` in.

## Fail-open, on purpose

`on_error: "open"`. This is orchestration discipline, not a security boundary — a crash in
the check must never wedge the ability to dispatch work.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x background_subagent_gate.py
echo '{"args":{"prompt":"'"$(python3 -c 'print("x"*300)')"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"block ...  exit=10  (long foreground dispatch)

echo '{"args":{"run_in_background":true,"prompt":"long..."}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (already background)

echo '{"args":{"agent_id":"sub-1","prompt":"long..."}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (subagent-exempt)
```
