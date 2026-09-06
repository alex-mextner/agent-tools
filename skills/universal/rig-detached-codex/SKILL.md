---
name: rig-detached-codex
description: >-
  Use when a codex orchestrator must dispatch a NON-TRIVIAL subagent as a truly detached
  child process (for in-process delegation codex has its own collaboration.spawn_agent, and
  the hook bridge already sees that child as a subagent). Run the bundled rig-detached-codex
  launcher - it nohup-detaches a `codex exec` child carrying RIG_AGENT_ID/RIG_DETACHED_AGENT
  so the codex hook bridge classifies every tool call in the child session as a dispatched
  subagent's, with hook trust so the child stays governed.
---

# rig-detached-codex — canonical detached-agent launcher for codex

The subagent-aware gates (`orchestrator-stays-thin`, `no-long-inline-process`,
`background-subagent-gate`) refuse orchestrator-level implementation work and long processes
on codex exactly as on Claude Code (agent-tools#573 — no harness is exempt). codex has two
sanctioned ways to delegate:

1. **In-process:** `collaboration.spawn_agent` + `collaboration.wait_agent`. codex tags every
   hook event inside the child thread with its own top-level `agent_id`/`agent_type`
   (verified on 0.153.4), and `lib/codex_hook_bridge` forwards that, so the child's tool calls
   pass every gate as a subagent's. No launcher needed.
2. **Detached child process:** this skill's launcher, for work that must outlive the tool call
   or run fully outside the parent session.

## Usage

```bash
~/.agents/skills/rig-detached-codex/rig-detached-codex <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the HANDOFF
  FILE PATH the child must write before exiting. May be relative to the caller's cwd.
- `[workdir]` — where the child runs (default: the caller's cwd).

The launcher returns immediately (the child is `nohup`-detached in its own session, stdin
from `/dev/null`); the child's stdout/stderr append to `/tmp/agent-logs/<name>.log`. Collect
the result by polling the handoff file named in the brief — there is no registry by design.

## What the child runs

`codex exec --dangerously-bypass-hook-trust --skip-git-repo-check -- <brief>`. The
hook-trust flag is NOT a bypass of the gates: `codex exec` runs no hooks at all without it
(verified on 0.153.4 — no PreToolUse/Stop hook fires in exec mode, even in a trusted project,
until the flag is passed). The rig-installed hooks are the vetted hook source the flag's help
text has in mind, so the flag is what makes the child GOVERNED by the subagent-side gates
(`subagent-no-bg-longproc` and friends).

## Where the launcher lives (the carrier)

This skill is a thin wrapper over the shared core in the sibling `rig-detached-agent`
universal skill (one launcher, three harnesses — the child argv is the only difference). rig
copies both skill dirs to `~/.agents/skills/` (default `skills_target`, default-on via
`skills.universal.all`) and links this one into `~/.codex/skills`; the wrapper resolves the
core relative to its own real path, so the provisioned command above works through the
symlink. `bin/` in the agent-tools repo is NOT a rig-discovered carrier.

## Why the process env is the identity source

See `rig-detached-agent/SKILL.md`: a running orchestrator cannot retroactively mutate its own
process environment — it can only set `RIG_AGENT_ID`/`RIG_DETACHED_AGENT` for a CHILD process,
which is exactly the sanctioned act of dispatching a subagent. Never `export` the markers by
hand in an interactive shell.
