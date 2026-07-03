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
  heredoc, OR an obvious build/edit (`sed -i`, `tee`, `npm`/`cargo`/`make` build, **`git commit`/
  `git push`**, **test runs** `pytest`/`go test`/`npm`/`bun`/`cargo test`). A bare `>`/`>>`
  redirect is **not** implementation on its own (`python foo.py > out.log` is allowed).
  Read-only inspection **and sanctioned orchestration** are **never** blocked — including a chain
  of **any length** where every segment's head is inspection (`git status`/`log`/`diff`/`show`/
  `branch`, `ls`, `cat`, `grep`, `find`, `head`, `tail`, `wc`, …) **or** orchestration
  (`gh pr list`/`view`/`checks`/`status`/`diff`, `gh run list`/`view`/`watch`, `gh api` **GET-only**
  — a mutation flag/`graphql` is not waved through, **`gh ship`**, `tg`, `review`, `git worktree
  list`), across `|`, `&&`, `;`, `||` or newline
  (`git status && ls`, `gh pr list && gh pr view 5`, `gh ship 5 && tg "shipped"`). This is the
  **flapping fix** (Alex tg#5743 / agent-tools#23): those orchestration chains used to trip the
  `>= 2 operators` rule and wrongly BLOCK. But a chain that merely *starts* allowed
  (`git status && sed -i ...`, `gh pr view && git commit`), or that mixes in any
  build/edit/heredoc segment, is judged on its **full** content — not waved through on its prefix.
  Judgement is per-segment-**head**: a build token used only as an argument/needle of an allowed
  command (`cat tee.log`, `grep cargo notes`, `git log | rg gh`) stays allowed; only a
  build/edit/commit *at a segment head* counts.

## Per-repo opt-out (Alex tg#5743)

Default **ON** (opt-OUT — this gate has always been always-on, so an un-enrolled repo keeps
firing, no regression). A repo that legitimately does inline work on main (e.g. `3d-cli`)
exempts itself:

- `agent_hooks.orchestrator_only: false` in the repo's committed `rig.yaml` (rig-provisioned), or
- `RIG_ORCHESTRATOR_ONLY=0` (session/CI override).

This mirrors the opt-IN per-repo knob of the sibling `worktree-only-writes` guard.

**Release carve-out — `gh ship` (#159):** the gated merge is the ONE repo mutation that belongs
at *orchestrator* altitude — the auto-mode classifier denies it inside subagents (a subagent
relaying its own merge is what the gates exist to stop), so blocking it here deadlocked the
release path entirely (observed live as warn-then-block *flapping* on repeated ships). A
`gh ship` **release chain** is therefore never warned or blocked: at least one segment head is
`gh ship` (env-var prefixes allowed: `GH_PAGER=cat gh ship 605`) and **every** segment head is
`gh ship`, read-only inspection, or `cd` — e.g. `gh ship 605 2>&1 | tail -30 | grep -i merged`,
`cd /repo && gh ship 605 | tail -40`. Same per-segment-head discipline as above: `gh ship` as a
substring/needle (`grep 'gh ship' log`) exempts nothing, and a ship segment does **not** launder
an implementation chain (`sed -i ... && gh ship`, `npm run build && gh ship`, any heredoc still
block). Build/edit tokens **and `$()`/backtick substitutions anywhere in the line** veto the
carve-out wholesale (a substitution can smuggle any mutation into a benign-looking segment), so
`gh ship 605 | tee ship.log` does **not** ride it — log a ship with a bare redirect instead
(`gh ship 605 > ship.log 2>&1`, allowed as a plain redirect). Only `gh ship` rides the *release*
carve-out — no other gh subcommand rides *that* one.

**Report / verify carve-out — `tg` + read-only gh reads (coordinator):** the orchestrator's role is
literally *verify + report*, so reporting to the user and read-only PR/CI verification must **not**
require a subagent. A line whose every segment head is `tg`, a read-only gh read
(`gh pr list/view/checks/status/diff`, `gh run list/view`), read-only inspection (incl. system-info
`df`/`du`/`lsblk`/`free`/`ps`/… and filters `jq`/`sort`/`cut`/…), or `cd` — with at least one `tg`/
gh-read head — is never warned or blocked, of **any** length: `tg --format html '…' | tail -3 | grep
merged`, `gh pr view 5 | jq .title | head`, `tg done; gh pr view 5; gh run list`. Same per-segment
discipline as the release carve-out — a build/edit, heredoc, substitution, bare-`&`, or mutating
companion forfeits it (`tg done && sed -i …`, `gh pr view && git push` are still implementation).
**`curl` and `ssh` are deliberately NOT sanctioned** — `curl -X POST`/`-d` mutates and `ssh host
'…'` runs any remote command, so neither can be reliably classified read-only; use the escape hatch
or a subagent for those.

**Subagent-exempt:** a dispatched subagent (`agent_id` present) does the actual work, so it is
always allowed. This gate governs the orchestrator only. Because the hook uses `agent_id` to
**relax**, it reads **only** the sanitized `args.agent_id` — never a top-level `event.agent_id`
fallback, and never model-controlled `tool_input`. `cc_hook_bridge` forwards `args.agent_id` only
from CC's authoritative top-level event and drops any `tool_input`-forged copy (T2 precedence), and
never writes a top-level `agent_id`; a non-CC carrier wiring this hook must replicate that filtering
or a forged `agent_id` self-exempts the orchestrator (see `background-subagent-gate/README.md` for
the full contract). This matches the sibling `skills-read-gate`'s narrowed read (agent-tools#115).

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
