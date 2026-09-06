---
name: rig-detached-omp
description: >-
  Use when an omp (oh-my-pi) orchestrator must dispatch a NON-TRIVIAL subagent as a truly
  detached child process (for in-process delegation omp has its own `task` tool, and the hook
  bridge already sees that child as a subagent). Run the bundled rig-detached-omp launcher -
  it nohup-detaches an `omp -p` child carrying RIG_AGENT_ID/RIG_DETACHED_AGENT so the omp hook
  bridge classifies every tool call in the child session as a dispatched subagent's.
---

# rig-detached-omp — canonical detached-agent launcher for omp

The subagent-aware gates (`orchestrator-stays-thin`, `no-long-inline-process`,
`background-subagent-gate`) refuse orchestrator-level implementation work and long processes
on omp exactly as on Claude Code (agent-tools#573 — no harness is exempt). omp has two
sanctioned ways to delegate:

1. **In-process:** the `task` tool. omp runs the child agent in the same process and invokes
   the hook-bridge extension once more for it; the extension tags that child session's tool
   calls with its session id (`lib/omp_hook_bridge/extension.ts`, verified on 18.0.11), so
   they pass every gate as a subagent's. No launcher needed.
2. **Detached child process:** this skill's launcher, for work that must outlive the tool call
   or run fully outside the parent session.

## Usage

```bash
~/.agents/skills/rig-detached-omp/rig-detached-omp <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the HANDOFF
  FILE PATH the child must write before exiting. May be relative to the caller's cwd.
- `[workdir]` — where the child runs (default: the caller's cwd).

The launcher returns immediately (the child is `nohup`-detached in its own session, stdin
from `/dev/null` — `omp -p` otherwise sits in `readPipedInput` forever under a harness bash
tool); the child's stdout/stderr append to `/tmp/agent-logs/<name>.log`. Collect the result by
polling the handoff file named in the brief — there is no registry by design.

## What the child runs

`omp -p --auto-approve -- <brief>`. A detached child cannot answer an approval prompt, so
tool calls are auto-approved; the agent-hooks are the guards (the same reasoning rig applies
to harness auto-mode).

## Where the launcher lives (the carrier)

This skill is a thin wrapper over the shared core in the sibling `rig-detached-agent`
universal skill (one launcher, three harnesses — the child argv is the only difference). rig
copies both skill dirs to `~/.agents/skills/` (default `skills_target`, default-on via
`skills.universal.all`), which omp's `agents` discovery provider scans natively; the wrapper
resolves the core relative to its own real path. `bin/` in the agent-tools repo is NOT a
rig-discovered carrier.

## Why the process env is the identity source

See `rig-detached-agent/SKILL.md`: a running orchestrator cannot retroactively mutate its own
process environment — it can only set `RIG_AGENT_ID`/`RIG_DETACHED_AGENT` for a CHILD process,
which is exactly the sanctioned act of dispatching a subagent. Never `export` the markers by
hand in an interactive shell.
