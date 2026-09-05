# subagent-no-monitor

**Point:** `pre-monitor` · **Fail policy:** `open` · **Priority:** 36

Stops a **dispatched subagent** from calling the **Monitor** tool at all (HYP-1350's
retrospective, hyperide/hyper-saas PR #754). The wedge: a subagent started a Monitor watch on
its own spawned child process, then ended its turn saying *"I'll wait for the completion
notification."* But a **subagent is NOT re-invoked by a Monitor-event notification — only the
main loop is** (Monitor has no harness-tracked child at all, unlike an ordinary backgrounded
Bash command — see "The correct alternative" below). So it idles forever with uncommitted work
and no PR — the exact same failure `subagent-no-bg-longproc` exists to stop for a *labeled*
long process, just reached through a different CC tool that gate doesn't inspect.

## Where it sits in the doctrine (sibling of subagent-no-bg-longproc)

| Gate | Governs | Rule |
| --- | --- | --- |
| `subagent-no-bg-longproc` (pre-bash) | a **subagent** | run your **own** long Bash work in the **foreground** — don't background it |
| **`subagent-no-monitor`** (pre-monitor) | a **subagent** | don't call **Monitor** at all — it has no foreground mode, so there's nothing to fall back to except a bounded synchronous poll |

The orchestrator's own Monitor use (e.g. watching a backgrounded subagent's progress) is
**unaffected** — this gate only fires on a tool call made INSIDE a dispatched subagent
(`agent_id` present).

## Why unconditional (no backgrounded/foreground classification)

`subagent-no-bg-longproc` has to classify: is the command long, AND is it backgrounded? A
subagent running `review` in the foreground is fine. Monitor has no equivalent foreground
mode — the tool's entire contract is "start it, keep working, get notified later." So there is
no shape of Monitor call from a subagent that isn't the wedge: the block is **any subagent
call to Monitor, full stop**, regardless of what it watches or why.

## The correct alternative

Which replacement applies depends on what the subagent is waiting for:

1. **A single ORDINARY command it just started** — one that is NOT itself a labeled long
   process (`review`, `--watch`, a build-test suite, a long `sleep` — those go to option 2
   instead, since `subagent-no-bg-longproc` blocks backgrounding them) — set
   `run_in_background: true` on the Bash tool call. The harness auto-resumes the subagent when
   that command completes; no Monitor is needed for this shape at all.
2. **Anything else** — a labeled long process, a condition to become true, a file to appear,
   several things at once —
   block on it **synchronously**, in the foreground, with a heartbeat loop: echo a line at
   least every ~20s and keep each Bash call comfortably under ~540s (well inside the Bash
   tool's own 600s hard cap), repeating the same bounded call until the wait is over:

```bash
timeout 540 bash -c 'i=0; until <condition-check> || [ "$i" -ge 26 ]; do sleep 20; i=$((i+1)); echo "[wait] tick $i ($((i*20))s)"; done'
```

Both shapes pass `subagent-no-bg-longproc`'s own rules too (verified): a foreground heartbeat
loop is not backgrounded, and that sibling gate only blocks `run_in_background: true` when the
*backgrounded* command is itself a long-process label (`review`/`--watch`/a build-test
suite/a long `sleep`) — an ordinary single command backgrounded this way is unaffected.

## No self-service bypass — external Telegram approval only

Same deny-by-default contract as `subagent-no-bg-longproc`: no env-var self-grant. For a
genuine exception, ASK the human, or request one-time Telegram approval:

```bash
RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR="self-managed watchdog, will poll for the result inline"
```

Set in the process environment before the tool call — Monitor's `tool_input` is a watch
target (what to observe, e.g. a `description` of the thing being watched), not an invoked
shell command line, so there is no leading `VAR=value` prefix a pre-monitor hook could parse
out of it the way `subagent-no-bg-longproc` does for a real Bash command string. Only the
process-env source applies here, matching how the pre-write hooks read this var. If unset, no
Telegram call is made and the call simply blocks. If present but blank/bare (`1`/`true`/`yes`),
the hook denies without contacting Telegram.

## Registration gap (read before assuming this fires)

Mapping `Monitor` → `pre-monitor` in `lib/cc_hook_bridge/dispatch.py` is **not**, on its own,
enough for CC to ever invoke the bridge for a `Monitor` tool call — Claude Code only runs
`PreToolUse` hooks it has an explicit **matcher** for in `settings.json`, and matchers are
written by **rig-cli**'s `hook_bridge_entries` (a separate repo), not by this dispatcher. This
is the identical two-repo split `pre-agent` and `pre-skill` went through. Until rig-cli's
`Monitor` matcher change ships **and** `rig apply` (or an equivalent manual `settings.json`
edit) runs on a given machine, this descriptor is installed but inert — CC never calls the
bridge for `Monitor` at all, so `subagent_no_monitor.py` never runs.

## Fail-open, on purpose

`on_error: "open"`. Anti-wedge / responsiveness discipline, not a security boundary — a crash
in the check must never wedge a subagent's ability to call a tool. An unparseable event
likewise allows.

## Test

```bash
chmod +x subagent_no_monitor.py

# subagent calls Monitor → BLOCK
echo '{"args":{"agent_id":"sub-1","description":"watch tests"}}' | ./subagent_no_monitor.py
rc=$?; echo "exit=$rc"   # → "decision":"block" ...  exit=10

# orchestrator calls Monitor (no agent_id) → allow
echo '{"args":{"description":"watch subagent progress"}}' | ./subagent_no_monitor.py
rc=$?; echo "exit=$rc"   # → "decision":"allow"  exit=0
```
