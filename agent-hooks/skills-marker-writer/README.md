# skills-marker-writer

**Point:** `pre-skill` · **Fail policy:** `open` · **Priority:** 50

Touches `~/.cache/agent-tools/skills-invoked/<skill-name>` on every Skill-tool invocation.
This is the wrapper `skills-read-gate` has always needed: that gate blocks/warns on
work-shaped actions unless the mandatory skills were invoked recently, deciding "recently"
by checking mtime freshness on exactly this marker. Before this hook existed, nothing ever
wrote the marker, so the freshness check always saw a missing file and the gate could never
leave its WARN-forever tier — see `agent-hooks/skills-read-gate/README.md`.

## What it does

On a `Skill`-tool `PreToolUse` event, reads the invoked skill name from `args.skill` and:

```bash
mkdir -p "${SKILLS_INVOKED_DIR:-$HOME/.cache/agent-tools/skills-invoked}"
touch "${SKILLS_INVOKED_DIR:-$HOME/.cache/agent-tools/skills-invoked}/<skill-name>"
```

(`Path.touch(exist_ok=True)` on an already-existing file already bumps its mtime — that IS
the freshness update the marker contract needs, no extra step required.)

`SKILLS_INVOKED_DIR` is the same env var `skills-read-gate` reads — the two hooks must agree
on the marker directory, so keep them configured together. This is a per-Skill-invocation
hook (a fresh Python process per call, like every other `agents-hooks/v1` hook), so
`timeout_ms` is 800, not the more common 5000 — a marker-write must never noticeably delay a
Skill call on a slow filesystem, and this hook's whole job (one filesystem write) fits well
inside that budget.

**Fires on the attempt, not the outcome.** The point is `PreToolUse`, so the marker is
written when the skill is *invoked*, before CC resolves/runs it — there is no reliable
success/failure signal to gate on at this point in the tool lifecycle. In practice a typo'd
or nonexistent skill name still writes a marker, but under that WRONG name, so
`skills-read-gate`'s freshness check (keyed to the mandatory skill's own name) is unaffected.
The only way this over-certifies is the correct name being invoked and the underlying skill
itself failing to load — accepted, since this whole mechanism is advisory (WARN-first,
fail-open), not a security boundary.

## Never blocks

Every code path emits `allow`. A marker-write failure (permissions, a race, an odd
filesystem) is logged to stderr and otherwise ignored — recording a skill invocation is
advisory bookkeeping, not a gate, and it must never be able to stop a skill from running.

## Untrusted input handling

`args.skill` is model-controlled: the agent decides which skill to invoke, and a legitimate
directory-scoped skill name contains a `/` (e.g. `apps/web:deploy`). A `/`-containing name is
therefore allowed and nests under the marker dir (`mkdir -p` handles the subdirectory) — but
an absolute path, a `..` path segment, an embedded NUL, or an oversized value (>200 chars) is
rejected outright: nothing it writes may resolve outside `SKILLS_INVOKED_DIR`. The resolved
write target is checked against the resolved marker dir as a second, independent guard on top
of the name-level sanitization, in case a future caller skips that check.

## Per-harness status

Claude Code: live once `rig` registers a `Skill` `PreToolUse` matcher for the bridge
(`riglib` `hook_bridge_entries`; the bridge half — the `pre-skill` point mapping in
`lib/cc_hook_bridge/dispatch.py` — ships in the same change as this hook). Codex/opencode:
not mapped yet — see `agent-hooks/README.md`'s `pre-skill` section for the current
per-harness table (the same gap the `pre-agent` point had before its bridges were wired).

## Fail-open, on purpose

`on_error: "open"`. A crash here must never wedge the ability to invoke a skill — worst
case, `skills-read-gate` simply doesn't see a fresh marker for that skill this session.

## Test

```bash
uv run --with pytest python -m pytest tests/test_skills_marker_writer.py -q
```
