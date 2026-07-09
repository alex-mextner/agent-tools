# opencode-hook-bridge - fire `agents-hooks/v1` hooks inside opencode

opencode has a native plugin surface, but it does not read the agent-tools
`agents-hooks/v1` descriptor directory by itself. This bridge is the carrier rig
symlinks into `~/.config/opencode/plugins/` so the same installed descriptors can
run under opencode.

## What it does

The JavaScript plugin handles:

```
tool.execute.before
tool.execute.after
```

For each event it invokes:

```
python3 -m opencode_hook_bridge <event>
```

The Python dispatcher reads the plugin payload from stdin, maps it to a logical v1
point, runs matching descriptors from `~/.config/opencode/hooks/*.json`, and returns
plain block JSON to the plugin:

```json
{"decision":"block","reason":"..."}
```

The plugin throws `Error(reason)` when it receives a block decision, which is the
opencode plugin contract for blocking a `tool.execute.before` call.

## Mapping

| opencode plugin event/tool | v1 point | Notes |
| --- | --- | --- |
| `tool.execute.before` + `bash` | `pre-bash` | `output.args.command` becomes `event.command` and `args.command`. |
| `tool.execute.before` + `edit` / `write` | `pre-write` | `output.args.filePath` is normalized to `args.file_path` / `args.path`; proposed content is normalized to `args.content`. |
| `tool.execute.before` + `apply_patch` | `pre-write` | `output.args.patchText` becomes raw `args.patch`; added patch lines become `args.content`; patch marker paths become `args.file_path` / `args.path`. |
| `tool.execute.before` + `task` | `pre-agent` | Carries the task payload (`subagent_type`, `prompt`, `description`) for orchestration guards. |
| `tool.execute.after` + `edit` / `write` / `apply_patch` | `post-write` | Runs path-based hooks after the write; exit 10 is surfaced as plugin feedback. |

opencode has session events such as `session.idle`, but this bridge does not map
them to the v1 `stop` point: opencode documents them as plugin notifications, not
as a pre-stop blocking contract.

For multi-file `apply_patch` calls, the dispatcher fans out one v1 event per file
path. If one patch contains multiple separate blocks for the same file path, the
per-file `args.content` value reflects the last block seen for that path.

The bridge strips `agent_id` / `agent_type` from opencode tool arguments. Those
fields are not trusted as a non-forgeable subagent identity in the plugin payload,
so subagent-exempt hooks treat opencode tool calls as main-thread calls unless a
future opencode contract exposes an authoritative identity field.

## Descriptor directory

By default the bridge reads:

```
~/.config/opencode/hooks
```

Tests and manual runs can override it:

```
OPENCODE_HOOKS_DIR=/tmp/hooks python3 -m opencode_hook_bridge tool.execute.before
```

## Installation (via rig)

Rig provisions two opencode paths when `harness.kind: opencode` and
`harness.hook_bridge.enabled` are active:

- descriptor JSON files in `~/.config/opencode/hooks`
- this plugin symlinked into `~/.config/opencode/plugins/agent-tools-hook-bridge.js`

opencode loads plugins only at startup, so restart opencode after `rig apply`.

## Fail policy

The top-level dispatcher fails open: malformed stdin, a dispatcher bug, or an
unreadable descriptor directory logs to stderr and allows the opencode action. A
loaded descriptor still honors v1 semantics: exit 10 blocks, exit 0 allows, and
other hook failures resolve through the descriptor's `on_error` policy.
