# codex-hook-bridge - fire `agents-hooks/v1` hooks inside Codex

Codex CLI has native hook events, but it does not read the agent-tools
`agents-hooks/v1` descriptor directory by itself. This bridge is the dispatcher Codex
hook commands call so the same installed descriptors can run under Codex.

## What it does

Codex calls the bridge once per configured hook event:

```
python3 -m codex_hook_bridge PreToolUse
python3 -m codex_hook_bridge PostToolUse
python3 -m codex_hook_bridge Stop
```

The bridge reads Codex's hook JSON from stdin, maps it to a logical v1 point, runs the
matching descriptors from `~/.codex/hooks/*.json`, and translates v1 exit code 10 into
Codex's plain block JSON:

```json
{"decision":"block","reason":"..."}
```

## Mapping

| Codex event/tool | v1 point | Notes |
| --- | --- | --- |
| `PreToolUse` + `Bash` | `pre-bash` | `tool_input.command` becomes `event.command` and `args.command`. |
| `PreToolUse` + `apply_patch` | `pre-write` | `tool_input.command` becomes raw `args.patch` / `args.command`; `args.content` is the added patch content with leading `+` markers stripped; each patch target is exposed to hooks as `args.file_path` / `args.path`. |
| `PostToolUse` + `apply_patch` | `post-write` | Runs path-based hooks once per patch target; carries raw `args.patch` but no synthetic `args.content`; exit 10 is feedback surfaced through Codex's block JSON shape. |
| `Stop` | `stop` | Carries the session id when Codex provides one. |

Multi-file `apply_patch` calls expose `args.file_paths` in the base translated event and
fan out descriptor execution so existing singular-path hooks receive one `args.file_path`
/ `args.path` at a time. That keeps current path-scoped hooks useful without changing the
v1 event contract.

The bridge strips `agent_id` / `agent_type` from Codex `tool_input`. Those fields are not
trusted until Codex has a captured, non-forgeable subagent identity fixture. Until that
exists, subagent-exempt `pre-bash` hooks treat Codex calls as main-thread calls.

Patch paths are extracted from patch text and are hints, not a trust boundary. Hook authors
that use `args.file_path` / `args.path` for filesystem access must resolve and validate the
path against the intended repository root before reading or writing.

## Not mapped yet: pre-agent

Codex has native `SubagentStart` / `SubagentStop` events, but this bridge does **not** map
them to `pre-agent` yet. The bridge needs a trustworthy captured Codex payload fixture
before it can safely decide which fields represent the dispatch prompt, background mode,
and non-forgeable subagent identity. Until then, `pre-agent` remains a documented gap for
Codex rather than a guessed mapping.

## Descriptor directory

By default the bridge reads:

```
~/.codex/hooks
```

Tests and manual runs can override it:

```
CODEX_HOOKS_DIR=/tmp/hooks python3 -m codex_hook_bridge PreToolUse
```

## Manual hook shape

Current Codex accepts TOML hook entries shaped like:

```toml
[hooks]
PreToolUse = [
  { matcher = "Bash", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PreToolUse" }] },
  { matcher = "apply_patch", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PreToolUse" }] },
]
PostToolUse = [
  { matcher = "apply_patch", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PostToolUse" }] },
]
Stop = [
  { hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge Stop" }] },
]
```

Rig wiring is a separate consumer concern; this package is only the stdlib dispatcher.

## Fail policy

The top-level dispatcher fails open: malformed stdin, a dispatcher bug, or an unreadable
descriptor directory logs to stderr and allows the Codex action. A loaded descriptor still
honors v1 semantics: exit 10 blocks, exit 0 allows, and other hook failures resolve through
the descriptor's `on_error` policy.
