---
name: rig-detached-opencode
description: >-
  Use when an opencode orchestrator must dispatch a NON-TRIVIAL subagent in the background
  on a default opencode build. That build has no native background field on its task tool
  (the field exists only behind OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true), and the
  background-subagent-gate pre-agent hook blocks a foreground dispatch. Run the bundled
  rig-detached-opencode launcher - it nohup-detaches an `opencode run` child carrying
  RIG_AGENT_ID/RIG_DETACHED_AGENT so the hook bridge classifies every tool call in the
  child session as a dispatched subagent.
---

# rig-detached-opencode — canonical detached-agent launcher for opencode

Since agent-tools#573 opencode has two sanctioned ways to delegate: the native Task tool
(the child session's tool calls pass every gate as a subagent's — the hook bridge identifies
it by opencode's own session `parentID`; foreground on a default build) and this launcher for
a truly detached child process.

The pre-agent gate `background-subagent-gate` blocks a non-trivial FOREGROUND subagent
dispatch: it wedges the main thread until the child finishes. Claude Code has real
background paths (`subagent_type: "fork"`, `isolation: "remote"`); a default opencode
build has NONE — its task tool carries no background field (verified on 1.18.20:
`description`/`prompt`/`subagent_type`/`task_id`/`command` only; the native
`background: true` exists only behind `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`,
and the bridge maps that field only when the hosting opencode process carries the flag).
The sanctioned background mechanism on a default build is this skill's bundled launcher.

## Where the launcher lives (the carrier)

This skill IS the carrier: a thin wrapper over the shared core in the sibling
`rig-detached-agent` universal skill (agent-tools#573: one launcher core, three harness
wrappers — `rig-detached-opencode` / `rig-detached-codex` / `rig-detached-omp`; the child
argv is the only difference). rig copies both skill dirs to the canonical skills dir —
`~/.agents/skills/rig-detached-opencode/rig-detached-opencode` (default `skills_target`,
default-on via `skills.universal.all`) — and opencode scans that dir natively; the wrapper
resolves the core relative to its own real path. `bin/` in the agent-tools repo is NOT a
rig-discovered carrier, so the launcher must not be referenced there: after `rig apply` on a
provisioned machine only the skill-copy path exists. Gate REMINDER texts name the provisioned
path.

## Usage

```bash
~/.agents/skills/rig-detached-opencode/rig-detached-opencode <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the
  HANDOFF FILE PATH the child must write before exiting. May be relative to the caller's
  cwd — the launcher reads its content BEFORE changing directories, so a relative brief
  with a different `[workdir]` still reaches the child in full.
- `[workdir]` — where the child runs (default: the caller's cwd).

The launcher returns immediately (the child is `nohup`-detached); the child's
stdout/stderr append to `/tmp/agent-logs/<name>.log`. Collect the result by polling the
handoff file named in the brief — the launcher keeps no registry by design.

## Why the process env is the identity source

The launcher exports `RIG_AGENT_ID=<name>` and `RIG_DETACHED_AGENT=1` into the child
opencode process. The opencode hook bridge (`lib/opencode_hook_bridge/dispatch.py`) strips
every model/tool-supplied `agent_id`-shaped key (forged args can never self-exempt) and
injects `args.agent_id` from these markers instead — through the shared
`lib/agent_hooks_v1/subagent_identity.py`, the same source the codex and omp bridges read —
so every subagent-exempt delegation gate (`orchestrator-stays-thin`,
`background-subagent-gate`, `no-long-inline-process`, `subagent-no-bg-longproc`) classifies
the child session's tool calls as a dispatched subagent's.

Never `export` these markers by hand in an interactive shell (or an rc file): every
opencode process that inherits them is treated as a dispatched subagent for its whole
session. The launcher sets them for the child process only.

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the
sanctioned act of dispatching a subagent. This matches the module family's
cooperative-orchestrator threat model (`on_error: open` discipline gates, not security
boundaries).
