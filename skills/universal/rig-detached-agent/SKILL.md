---
name: rig-detached-agent
description: >-
  The shared launcher core behind rig-detached-opencode, rig-detached-codex and
  rig-detached-omp. Never call it directly from an agent session - use the per-harness
  wrapper for the harness you are running in; this skill only has to be provisioned next to
  them (rig does that by default) so the wrappers can find the core.
---

# rig-detached-agent — the one detached-agent launcher core

`rig-detached-opencode`, `rig-detached-codex` and `rig-detached-omp` are thin wrappers that
`exec` this core with their harness name prepended:

```bash
~/.agents/skills/rig-detached-agent/rig-detached-agent <opencode|codex|omp> <name> <brief-file> [workdir]
```

Everything a launcher does is shared and lives here once (agent-tools#573): argument
validation, the private log (`$RIG_AGENT_LOG_DIR/<name>.log`, default `/tmp/agent-logs`,
dir 0700 + owner-checked, log 0600, symlink refused), reading the brief BEFORE `cd`, exporting
`RIG_AGENT_ID=<name>` + `RIG_DETACHED_AGENT=1` into the child, and the real detach (`nohup`,
a NEW SESSION via `setsid`/`os.setsid()`, stdin from `/dev/null`, `--` before the brief so a
brief starting with `---` or `- ` is never eaten as a flag). The only per-harness part is the
child argv:

| harness  | child command |
| --- | --- |
| opencode | `opencode run --title <name> -- <brief>` |
| codex    | `codex exec --dangerously-bypass-hook-trust --skip-git-repo-check -- <brief>` — `codex exec` runs NO hooks at all without that flag (verified on 0.153.4, even in a trusted project); the rig-installed hooks are the vetted source its help text has in mind, so the flag is what keeps the child GOVERNED |
| omp      | `omp -p --auto-approve -- <brief>` — a detached child cannot answer an approval prompt; the agent-hooks are the guards |

## Why the process env is the identity source

Every hook bridge (`lib/*_hook_bridge/dispatch.py`) reads the markers through the shared
`lib/agent_hooks_v1/subagent_identity.py` and injects `args.agent_id` on every tool call, so
every subagent-aware gate (`orchestrator-stays-thin`, `background-subagent-gate`,
`no-long-inline-process`, `subagent-no-bg-longproc`) classifies the child session as a
dispatched subagent. A running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the sanctioned
act of dispatching a subagent; model-controlled tool args can never self-exempt (the bridges
strip forged `agent_id` keys before the env injection).

Never `export` these markers by hand in an interactive shell (or an rc file): every harness
process that inherits them is treated as a dispatched subagent for its whole session.
