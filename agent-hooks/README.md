# Agent hooks (`agents-hooks/v1`)

Agent hooks are **out-of-process guards that fire on an agent's tool use** and decide
whether the action is allowed. Unlike a git hook (which fires at commit/push time), an
agent hook catches the action *mid-session* — the moment the agent tries to run a command,
write a file, or finish a turn. That makes them the right place for rules that must be
enforced *before* the side effect happens, not after.

Each hook's hook-specific files live in one directory:

```
<hook-name>/
  <id>.<point>.json   # the descriptor — what to run and when
  <script>            # the executable the descriptor points at
  README.md           # what it does, how to install, fail policy
```

Hooks that need the shared `agenttools_hatch_escalation` approval helper load it from this
catalog's repo-local `lib/` path with `importlib.util.spec_from_file_location`, not through
ambient `sys.path`. The bootstrap is duplicated inside those hook scripts because a hook
install must not depend on a shared helper file under `agent-hooks/`; installers must preserve
the catalog layout that keeps `lib/agenttools_hatch_escalation` two levels above those scripts
or rewrite the helper path deliberately. On successful load the hook keeps the repo-local helper
in `sys.modules` so a preloaded user/site package cannot regain control later in the same hook
process.

These scripts are catalog artifacts, not Python package entrypoints: do not install hatch-using
hooks by copying only the executable into `/usr/local/bin`, a virtualenv `bin/`, or another flat
script directory and expecting an installed site-package dependency to satisfy the helper import.
That fallback is intentionally absent because it would reintroduce dependency confusion through
ambient `sys.path`. The normal host model is one hook execution per subprocess; long-lived
in-process hosts must provide equivalent module isolation if they import hook scripts directly.

## The contract (`agents-hooks/v1`)

A host (the agent harness, or a tool like a CLI) runs the hook executable and speaks a
tiny JSON-on-stdin / exit-code protocol. No shared runtime between host and hook.

Hosts must execute hook scripts as separate subprocesses. Importing hook scripts directly into
a long-lived or multi-threaded host process is not part of the contract unless that host provides
its own equivalent process/module isolation.

- **stdin**: a JSON event — `{ hook_api, event_id, tool, point, harness, command, cwd, args }`.
  `args` carries the action-specific payload (e.g. the Bash command string, the file path
  + content for a write). `harness` is a bridge-set constant (`"claude-code"` / `"codex"` /
  `"opencode"`) identifying which product's hook system dispatched the event — set from a
  hardcoded module literal in each bridge, never derived from `args`/`tool_input`, so it cannot
  be forged the way a same-named key sitting in `args` could be (agent-tools#533). A hook that
  needs to scope itself to, or exempt, an entire harness reads `event["harness"]` directly; see
  `agent-hooks/orchestrator-stays-thin`'s `EXEMPT_HARNESSES` for the first consumer. **A hook
  that RELAXES on `harness` must use a fail-closed allowlist** (`harness in {known-safe values}`),
  never a `!=` exclusion: the field may be absent on an event from a bridge that predates it, or
  from any future bridge that doesn't set one, and an absent/unrecognized value must stay
  GOVERNED by default, exactly like this repo's `agent_id` subagent-exemption convention.
- **stdout**: protocol JSON only — `{ "hook_api": "agents-hooks/v1", "decision": "allow"
  | "block", "message": "..." }`. Empty stdout = allow.
- **stderr**: human logs (never parsed).
- **exit code is the canonical signal:**
  - `0` → allow
  - `10` → **BLOCK** (un-corruptible: exit 10 blocks even if stdout is malformed, so a
    broken gate can never silently let an action through)
  - any other exit → hook *error*, resolved by the descriptor's `on_error` policy, even if
    stdout is empty, malformed, or a Python traceback.

## Fail policy: `on_error`

- `on_error: "open"` (default) — a hook crash/timeout/bad-output **warns and proceeds**.
  Use for advisory hooks that must never break the agent's flow.
- `on_error: "closed"` — a hook error **blocks** the action. Use for security gates where
  "I couldn't check" must mean "don't do it".

If a hook cannot import its repo-local helper code (for example a missing/corrupt
`lib/agenttools_hatch_escalation`), it fails before emitting protocol JSON. That is still a
hook error: the host/bridge must apply the descriptor's `on_error` policy to decide whether
the action proceeds.

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
> fire: rig wires it into `settings.json` (PreToolUse for `pre-bash`/`pre-write`/`pre-agent`/
> `pre-skill`, PostToolUse for `post-write`, Stop for `stop`) and translates the exit-10
> BLOCK into CC's `permissionDecision: "deny"` / `decision: "block"`. Without that bridge
> these hooks are inert in CC (agent-tools#18).

> **Codex:** Codex also needs a carrier bridge. `lib/codex_hook_bridge` is the first
> dispatcher for the confirmed Codex hooks contract: TOML hooks call it for `PreToolUse`
> `Bash` (`pre-bash`), `PreToolUse` `apply_patch` (`pre-write`), `PostToolUse`
> `apply_patch` (`post-write` feedback), and `Stop` (`stop`). It reads descriptors from
> `~/.codex/hooks` and emits Codex's plain `{"decision":"block","reason":"..."}` shape.
> Codex `pre-agent` is **not** mapped yet: `SubagentStart` / `SubagentStop` need a
> trustworthy payload fixture before this catalog can safely enforce that point.

> **opencode:** opencode also needs a carrier bridge. `lib/opencode_hook_bridge` ships a
> JavaScript plugin plus Python dispatcher: rig symlinks the plugin into
> `.opencode/plugins/zz-agent-tools-hook-bridge.js`, and the plugin calls the dispatcher for
> `tool.execute.before` (`bash` -> `pre-bash`, `edit`/`write`/`apply_patch` -> `pre-write`,
> `task` -> `pre-agent`) and `tool.execute.after` file edits (`post-write`). It reads
> descriptors from `~/.config/opencode/hooks`; pre-tool exit 10 blocks by throwing an opencode
> plugin error, while post-write exit 10 is logged as feedback because the write already
> landed. opencode `stop` is **not** mapped: documented session
> events such as `session.idle` are notifications, not a pre-stop block contract.

| point          | fires when…                                  | hooks                                   |
| -------------- | -------------------------------------------- | --------------------------------------- |
| `pre-agent` **(live in Claude Code and opencode when rig provisions their bridges; NOT mapped in Codex yet)** | before a subagent dispatch (Agent/Task/opencode task tool) | background-subagent-gate                 |
| `pre-bash`     | before a shell command runs                  | block-devserver-primary, block-no-verify, block-raw-pr-merge, block-reset-hard, pin-primary-worktree, pkill-guard, require-review-before-commit, require-ticket-before-commit, enforce-timeout-on-bash, orchestrator-stays-thin, no-long-inline-process, subagent-no-bg-longproc, no-shell-file-edit, skills-read-gate, visual-proof-gate, decision-request-format, heavy-op-memory-gate |
| `pre-write`    | before a file write/edit                     | block-secrets-write, block-raw-process-env, orchestrator-stays-thin, worktree-only-writes |
| `pre-skill` **(live in Claude Code when rig provisions the bridge; NOT mapped in Codex/opencode yet)** | before a Skill-tool invocation | skills-marker-writer |
| `pre-monitor` **(mapped by the bridge; NOT yet wired to a rig-cli matcher — inert until that ships, see below)** | before a Monitor-tool call | subagent-no-monitor |
| `post-write`   | after a file write/edit has landed on disk   | format-on-write, lint-on-write          |
| `stop`         | when the agent is about to end its turn      | stop-completion-selfcheck               |

### `pre-agent` — gate a subagent dispatch

`pre-agent` fires before the orchestrator dispatches a subagent (CC's `Agent`/`Task` tool),
so a hook can shape *how* work is fanned out — block a non-trivial **foreground** dispatch and
require `run_in_background: true` (or a Workflow). The bridge maps it from CC's `PreToolUse`
on `Agent`/`Task` and forwards CC's `agent_id`/`agent_type` (present only inside a dispatched
subagent) into `args`, so a subagent-exempt gate can tell the orchestrator's dispatch apart
from a worker's. opencode maps its `task` tool to the same logical point, but does not expose
a trusted subagent identity in the plugin payload, so forged `agent_id` / `agent_type` values
inside tool args are stripped.

### `pre-skill` — record a skill invocation

`pre-skill` fires before a Skill-tool call runs (CC's `PreToolUse` on the `Skill` tool). It
exists for exactly one consumer today: `skills-marker-writer`, which touches
`~/.cache/agent-tools/skills-invoked/<session-id>/<skill-name>` (session-scoped by CC's own
session id, so one session invoking a skill can't satisfy another concurrent session's gate)
so `skills-read-gate`'s freshness check (on `pre-bash`) has something real to read. A
`pre-skill` hook should never block — the point is a recording tap, not a gate — so hooks
here should be `on_error: open` and always emit `allow`.

### `pre-monitor` — block a subagent's Monitor call

`pre-monitor` fires before CC's `Monitor` tool call runs (the fire-and-forget background
event-stream watch — start it, keep working, get notified per line/event later). Its one
consumer, `subagent-no-monitor`, blocks it **unconditionally** whenever `agent_id` is present
(a dispatched subagent), because a subagent is never re-invoked by a later notification — only
the main loop is. The orchestrator's own Monitor use is unaffected. **Registration gap:** the
point mapping lives in `lib/cc_hook_bridge/dispatch.py`, but CC only fires a `PreToolUse` hook
for a tool it has an explicit `settings.json` matcher for — that matcher is written by
rig-cli's `hook_bridge_entries` (a separate repo, same split `pre-agent`/`pre-skill` went
through) and requires `rig apply` (or an equivalent manual edit) to take effect on a given
machine.

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
