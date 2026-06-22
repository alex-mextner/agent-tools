# orchestrator-stays-thin

**Points:** `pre-write` + `pre-bash` · **Fail policy:** `open` · **Priority:** 45

The orchestrator plans, dispatches, and verifies — it does **not** implement inline. When the
**main thread** is about to do implementation-shaped work itself, this gate nudges it to
delegate to a subagent or a Workflow. Enforces `delegate-work-to-subagents`.

One script binds two points via two descriptors; it branches on `event["point"]`:

- **`pre-write`** — a **code** Edit/Write (non-docs) by the main thread → warn-then-block.
  Docs are exempt: a path matching `\.(md|mdx|txt|rst)$` or under `docs/` is always allowed.
- **`pre-bash`** — a clearly **multi-step / implementation-shaped** Bash by the main thread →
  warn-then-block. Implementation-shaped = chained `> 2` steps (`&&`/`;`/`||`/`|`/newline), OR a
  heredoc, OR an obvious build/edit (`sed -i`, `tee`, `npm`/`cargo`/`make` build). A bare
  `>`/`>>` redirect is **not** implementation on its own (`python foo.py > out.log` is allowed).
  A read-only inspection command is **never** blocked — including a **fully read-only chain of
  any length** where every segment's head is inspection (`git status`/`log`/`diff`/`show`/`branch`,
  `ls`, `cat`, `grep`, `find`, `head`, `tail`, `wc`, …), across `|`, `&&`, `;`, `||` or newline
  (`find ... | grep ... | head`, `tail X | grep Y | wc -l`, `git status && ls && cat x`). But a
  chain that merely *starts* read-only (`git status && sed -i ...`), or that mixes in any
  build/edit/heredoc segment, is judged on its **full** content — not waved through on its prefix.
  Judgement is per-segment-**head**: a build token used only as an argument/needle of a read-only
  command (`cat tee.log`, `grep cargo notes`) stays allowed; only a build/edit *at a segment head*
  (`sed -i ...`, `tee ...`, `npm`/`cargo`/`make` build) counts.

**Subagent-exempt:** a dispatched subagent (`agent_id` present) does the actual work, so it is
always allowed. This gate governs the orchestrator only. `args.agent_id` must come from a
**trusted, transport-level** signal — never from model-controlled `tool_input`. `cc_hook_bridge`
forwards it only from CC's top-level event and drops any `tool_input`-forged copy; a non-CC
carrier wiring this hook must replicate that filtering or a forged `agent_id` self-exempts the
orchestrator (see `background-subagent-gate/README.md` for the full contract).

## Tiering — WARN then BLOCK

The **first** offense in the TTL window **WARNs** (allow + message); a **repeat** in the
window **BLOCKs**. The tier is a marker file keyed by a hash of `(cwd, point)`, so `pre-write`
and `pre-bash` tier **independently** — a write WARN does not prime a bash BLOCK (or vice versa):

- `ORCH_THIN_MARKER_DIR` — marker dir (default `~/.cache/agent-tools/orchestrator-thin`)
- `ORCH_THIN_TTL_S` — warn-suppression window in seconds (default `900`)

This delivers the doctrine's "WARN then BLOCK" rather than a hard wall on the first inline edit.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so a
> per-session env change is always picked up on the next call — this is fine, not a footgun.

## Escape hatch (controllable, not a hard wall)

```bash
# session-wide override (reason REQUIRED, or it still blocks) — works for BOTH points:
ALLOW_ORCHESTRATOR_WORK=1 ALLOW_ORCHESTRATOR_WORK_REASON="trivial config tweak, no subagent worth it"

# one-off, self-documenting — PRE-BASH ONLY:
sed -i 's/a/b/' file   # orchestrator-ok: one-char fix in a generated file
```

A reasonless `ALLOW_ORCHESTRATOR_WORK=1` is ignored and the action stays gated.

> **The inline `# orchestrator-ok:` sentinel only applies to `pre-bash`.** A `pre-write` (an
> Edit/Write tool call) carries no shell string for the comment to live in, so the inline hatch
> genuinely cannot fire for a write — for a write use the **env** hatch above.

## Fail-open, on purpose

`on_error: "open"`. Delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.

## Test

The hook's exit code is the canonical signal (`0` allow · `10` block). Capture it on its OWN
line right after the pipe so what's printed is the HOOK's exit, not `echo`'s:

```bash
chmod +x orchestrator_stays_thin.py
# first offense → WARN (allow + message); repeat in the window → BLOCK
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=0 (first offense, WARN)
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=10 (repeat → BLOCK)

echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/README.md"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # docs → exit=0 (allow)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"git status"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # single read-only → exit=0 (allow)
```
