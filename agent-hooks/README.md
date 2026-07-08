# Agent hooks (`agents-hooks/v1`)

Agent hooks are **out-of-process guards that fire on an agent's tool use** and decide
whether the action is allowed. Unlike a git hook (which fires at commit/push time), an
agent hook catches the action *mid-session* — the moment the agent tries to run a command,
write a file, or finish a turn. That makes them the right place for rules that must be
enforced *before* the side effect happens, not after.

Each hook here is a self-contained directory:

```
<hook-name>/
  <id>.<point>.json   # the descriptor — what to run and when
  <script>            # the executable the descriptor points at
  README.md           # what it does, how to install, fail policy
```

## The contract (`agents-hooks/v1`)

A host (the agent harness, or a tool like a CLI) runs the hook executable and speaks a
tiny JSON-on-stdin / exit-code protocol. No shared runtime between host and hook.

- **stdin**: a JSON event — `{ hook_api, event_id, tool, point, command, cwd, args }`.
  `args` carries the action-specific payload (e.g. the Bash command string, the file path
  + content for a write).
- **stdout**: protocol JSON only — `{ "hook_api": "agents-hooks/v1", "decision": "allow"
  | "block", "message": "..." }`. Empty stdout = allow.
- **stderr**: human logs (never parsed).
- **exit code is the canonical signal:**
  - `0` → allow
  - `10` → **BLOCK** (un-corruptible: exit 10 blocks even if stdout is malformed, so a
    broken gate can never silently let an action through)
  - any other exit → hook *error*, resolved by the descriptor's `on_error` policy.

## Fail policy: `on_error`

- `on_error: "open"` (default) — a hook crash/timeout/bad-output **warns and proceeds**.
  Use for advisory hooks that must never break the agent's flow.
- `on_error: "closed"` — a hook error **blocks** the action. Use for security gates where
  "I couldn't check" must mean "don't do it".

## Descriptor fields

```jsonc
{
  "id": "block-no-verify",          // stable id; [A-Za-z0-9._-]
  "point": "pre-bash",              // the host hook point this binds to
  "cmd": "/ABSOLUTE/PATH/TO/script",// MUST be absolute (the runner rejects relative/bare)
  "args": [],                       // extra argv before the JSON protocol (rare)
  "priority": 50,                   // lower runs first; default 50
  "timeout_ms": 5000,               // per-hook timeout; default 5000
  "on_error": "closed",            // "open" (default) | "closed"
  "description": "..."
}
```

> **`cmd` must be an absolute path.** The runner hashes the file at `cmd` but spawns it,
> and a bare/relative command would let the executed bytes differ from the hashed bytes.
> Installers substitute the real absolute path at install time; the descriptors here ship
> a `/ABSOLUTE/PATH/TO/...` placeholder.

## Hook points used here

These are the *logical* points; map them to your harness's actual tool-use events
(e.g. a PreToolUse matcher on the Bash/Write/Edit tool, or a Stop hook).

> **Claude Code:** CC does NOT run these descriptors directly — it only runs hooks declared
> in `settings.json`. The `lib/cc_hook_bridge` dispatcher is the carrier that makes them
> fire: rig wires it into `settings.json` (PreToolUse for `pre-bash`/`pre-write`, PostToolUse
> for `post-write`, Stop for `stop`) and translates the exit-10 BLOCK into CC's
> `permissionDecision: "deny"` / `decision: "block"`. Without that bridge these hooks
> are inert in CC (agent-tools#18).

> **Codex:** Codex also needs a carrier bridge. `lib/codex_hook_bridge` is the first
> dispatcher for the confirmed Codex hooks contract: TOML hooks call it for `PreToolUse`
> `Bash` (`pre-bash`), `PreToolUse` `apply_patch` (`pre-write`), `PostToolUse`
> `apply_patch` (`post-write` feedback), and `Stop` (`stop`). It reads descriptors from
> `~/.codex/hooks` and emits Codex's plain `{"decision":"block","reason":"..."}` shape.
> Codex `pre-agent` is **not** mapped yet: `SubagentStart` / `SubagentStop` need a
> trustworthy payload fixture before this catalog can safely enforce that point.

| point          | fires when…                                  | hooks                                   |
| -------------- | -------------------------------------------- | --------------------------------------- |
| `pre-agent` **(bridge-ready; NOT yet live in CC — needs the rig-cli `Agent\|Task` matcher; NOT mapped in Codex yet)** | before a subagent dispatch (Agent/Task tool) | background-subagent-gate                 |
| `pre-bash`     | before a shell command runs                  | block-no-verify, block-raw-pr-merge, block-reset-hard, require-review-before-commit, require-ticket-before-commit, enforce-timeout-on-bash, orchestrator-stays-thin, no-long-inline-process, subagent-no-bg-longproc, no-shell-file-edit, skills-read-gate, visual-proof-gate, decision-request-format |
| `pre-write`    | before a file write/edit                     | block-secrets-write, block-raw-process-env, orchestrator-stays-thin, worktree-only-writes |
| `post-write`   | after a file write/edit has landed on disk   | format-on-write, lint-on-write          |
| `stop`         | when the agent is about to end its turn      | stop-completion-selfcheck               |

### `pre-agent` — gate a subagent dispatch (bridge-ready; NOT yet live in CC — needs the rig-cli `Agent|Task` matcher)

`pre-agent` fires before the orchestrator dispatches a subagent (CC's `Agent`/`Task` tool),
so a hook can shape *how* work is fanned out — block a non-trivial **foreground** dispatch and
require `run_in_background: true` (or a Workflow). The bridge maps it from CC's `PreToolUse`
on `Agent`/`Task` and forwards CC's `agent_id`/`agent_type` (present only inside a dispatched
subagent) into `args`, so a subagent-exempt gate can tell the orchestrator's dispatch apart
from a worker's. For CC to fire this point, rig-cli must wire an `Agent|Task` PreToolUse
matcher (a rig-cli follow-up; the bridge half lives here).

### `post-write` — react to a *completed* write

`pre-write` is a **gate**: it sees the *proposed* bytes and can `block` before they touch
disk. `post-write` is the complement — it fires **after** the file has been written/edited,
when the file actually exists, so a hook can *react to* (not veto) the change. It's the
right point for anything that must operate on the real file: reformatting it, regenerating
a sibling, re-indexing it. The event payload still carries the written path (`args.path` /
`args.file_path`) plus `event.cwd`; there is no "proposed content" to inspect because the
content is already on disk. Hooks on this point should be `on_error: open` and treat the
work as advisory — the write already happened, so a *veto* after the fact is meaningless;
an exit-10 on this point is **feedback** (the bridge surfaces the hook's message to the
model via PostToolUse `{"decision": "block", "reason": …}`), which is how lint-on-write
reports findings. In CC it is live via the bridge's `PostToolUse` mapping (matcher
`Edit|Write|MultiEdit|NotebookEdit`, wired by rig).

## Installing

1. Edit the descriptor's `cmd` to the absolute path of the script (and `chmod +x` it).
2. Drop the descriptor into your harness's hook directory for the matching point.
3. Map the logical `point` to your harness's real event if its names differ.

Security note: a descriptor names an executable the host runs on every matching action.
Only you (or your own tooling) should write to the hook directory.

## Carrier choice

These are *agent* hooks specifically because they must intercept the action **mid-session**
— a git hook can't block a `--no-verify` commit (it's the very thing being skipped) and
can't stop a secret from being *written to a file* before the commit. See
`../docs/carrier-decision-guide.md` for when a rule belongs in a skill vs an agent-hook vs
a git-hook.
