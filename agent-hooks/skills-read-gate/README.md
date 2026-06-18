# skills-read-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 48

Before a **work-shaped action** — a `git commit`, or a build/test command — this checks that
the **mandatory skills were invoked** this session. A skill encodes the rules the action must
follow (e.g. `delegate-work-to-subagents` before dispatching, `visual-proof-cycle` before a
UI "done"); doing the work without reading the skill skips those rules.

## The marker contract (how it knows a skill was invoked)

A skill-invocation wrapper **touches one file per invoked skill** in a marker dir:

```
~/.cache/agent-tools/skills-invoked/<skill-name>     # mtime = invocation time
```

A skill counts as invoked if its marker is **fresh** (within `SKILLS_FRESH_WINDOW_S`,
default `7200`s). Wire the wrapper to touch it on every Skill-tool invocation:

```bash
mkdir -p "${SKILLS_INVOKED_DIR:-$HOME/.cache/agent-tools/skills-invoked}"
touch "${SKILLS_INVOKED_DIR:-$HOME/.cache/agent-tools/skills-invoked}/<skill-name>"
```

This is the honest, satisfiable action: invoking the skill writes its marker, which is what
the gate checks — it is real wiring, not a forge.

Configure via env:

- `SKILLS_INVOKED_DIR` — marker dir (default `~/.cache/agent-tools/skills-invoked`)
- `MANDATORY_SKILLS` — comma list (default `delegate-work-to-subagents,visual-proof-cycle`)
- `SKILLS_FRESH_WINDOW_S` — freshness window in seconds (default `7200`)
- `SKILLS_TIER_DIR` — warn/block tier marker dir (default `~/.cache/agent-tools/skills-read-tier`)

> The env-configured marker dirs are read at import time; CC re-invokes the script per call, so
> each call picks up the current env — this is fine, not a footgun.

## Tiering — WARN then BLOCK

If a mandatory skill has no fresh marker, the **first** work action WARNs (allow + message);
a **repeat** in the window BLOCKs (tracked by a cwd-keyed marker). The default WARN-first tier
means the gate never wedges while the marker-writer is still being wired everywhere.

## Not subagent-exempt

A subagent doing work should also have read its skills, so this gate does **not** exempt
subagents (unlike the orchestration gates 1–3).

## Escape hatch (controllable, not a hard wall)

```bash
ALLOW_SKIP_SKILLS=1 ALLOW_SKIP_SKILLS_REASON="docs-only commit, skills N/A"   # session-wide
git commit -m x   # skills-ok: trivial config bump
```

A reasonless `ALLOW_SKIP_SKILLS=1` is ignored and the action stays gated.

## Fail-open, on purpose

`on_error: "open"`. Process discipline, not a security boundary — a crash must never wedge
the ability to commit/build.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x skills_read_gate.py
# no fresh markers → first commit WARNs, the next BLOCKs
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (first offense, WARN)
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=10 (repeat → BLOCK)

# touch every mandatory skill's marker → allow
mkdir -p ~/.cache/agent-tools/skills-invoked
touch ~/.cache/agent-tools/skills-invoked/delegate-work-to-subagents ~/.cache/agent-tools/skills-invoked/visual-proof-cycle
echo '{"cwd":"/r","args":{"command":"git commit -m x"}}' | ./skills_read_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (markers fresh → allow)
```
