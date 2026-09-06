"""The agents-hooks/v1 -> omp (oh-my-pi) extension bridge dispatcher.

omp loads Bun-executed TS/JS extension modules that register handlers via
``pi.on(eventName, handler)``. The companion ``extension.ts`` file calls this module
for the ``tool_call`` (pre-execution) and ``tool_result`` (post-execution) events. This
dispatcher maps omp's flat tool-event payload to the shared agents-hooks/v1 contract
and returns a plain ``{"decision":"block"}`` JSON object to the extension.

Unlike the codex/opencode bridges, the extension NEVER throws on a bridge-infrastructure
failure (broken dispatcher, timeout, malformed JSON) — see ``extension.ts`` and its
README section "Fail policy" for why: omp's own contract is that ANY thrown error from a
``tool_call`` handler blocks the call, so a broken bridge would silently brick every tool
call in an interactive omp session. Only an explicit block decision from a hook is
translated into a block; every other failure here (and in the extension) fails open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"
# The v1 event's `harness` tag for every event this bridge produces — a MODULE LITERAL,
# never derived from `omp_event`, so it cannot be forged via tool args. See
# `codex_hook_bridge.HARNESS` / `opencode_hook_bridge.HARNESS` for the same reasoning: omp
# exposes no TRUSTED per-tool-call subagent identity either (forged agent_id/agent_type
# keys are stripped below). `agent-hooks/orchestrator-stays-thin`'s `EXEMPT_HARNESSES`
# reads this tag to exempt the whole harness (agent-tools#533).
HARNESS = "omp"

_KNOWN_EVENTS = frozenset({"tool_call", "tool_result"})
_WRITE_TOOLS = frozenset({"edit", "write", "apply_patch", "notebook"})
_TASK_TOOL = "task"

_FORGED_AGENT_KEYS = (
    "agent_id",
    "agent_type",
    "agentID",
    "agentId",
    "agentType",
    "agent",
)

# A hashline section header: `[RELATIVE/PATH#TAG]` where TAG is 4 uppercase hex chars.
_HASHLINE_HEADER = re.compile(r"^\[(?P<path>.+)#(?P<tag>[0-9A-F]{4})\]$")
# Only a `PUT ...:` operation header carries body (`+TEXT`) rows; CUT/REM/MV/register
# pastes never do (docs/tools/edit.md).
_HASHLINE_PUT_WITH_BODY = re.compile(r"^PUT\b.*:\s*$")


def point_for_event(event_name: str, tool_name: str | None) -> str | None:
    """Map an omp extension event/tool pair to a logical v1 point."""
    tool = (tool_name or "").lower()
    if event_name == "tool_call":
        if tool == "bash":
            return "pre-bash"
        if tool in _WRITE_TOOLS:
            return "pre-write"
        if tool == _TASK_TOOL:
            return "pre-agent"
    if event_name == "tool_result" and tool in _WRITE_TOOLS:
        return "post-write"
    return None


def to_v1_event(omp_event: dict, *, point: str) -> dict:
    """Translate the omp extension payload into agents-hooks/v1."""
    tool = _tool_name(omp_event)
    raw_args = _tool_args(omp_event)
    args = dict(raw_args)
    # Normalize BEFORE stripping: `agent` is both the source field for `subagent_type` and a
    # forged-identity key that must not survive into `args` itself.
    _normalize_task_args(args)
    for key in _FORGED_AGENT_KEYS:
        args.pop(key, None)

    command = _tool_command(tool, raw_args)
    if command:
        args["command"] = command

    if point in ("pre-write", "post-write"):
        _normalize_write_args(tool, raw_args, args, point=point)

    return {
        "hook_api": HOOK_API,
        "event_id": _event_id(omp_event),
        "tool": tool,
        "point": point,
        "harness": HARNESS,
        "command": command,
        "cwd": _cwd(omp_event),
        "args": args,
    }


def omp_block_output(reason: str) -> dict:
    """The extension consumes this shape and returns ``{block: true, reason}`` to omp."""
    return {"decision": "block", "reason": reason}


def hooks_dir() -> Path:
    """Where omp-installed v1 descriptors live, overridable for tests.

    Precedence: ``OMP_HOOKS_DIR`` (test/session override, highest priority) > the omp agent
    root resolved the same way rig-cli's ``riglib.harness_skills.omp_agent_root`` resolves
    it (``PI_CODING_AGENT_DIR`` full override > ``PI_CONFIG_DIR`` config-dirname rename >
    default ``~/.omp/agent``) > ``hooks``.
    """
    override = os.environ.get("OMP_HOOKS_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser(_omp_agent_root())) / "hooks"


def _omp_agent_root() -> str:
    """omp's agent dir (unexpanded, ``~``-anchored), honoring omp's own env overrides.

    Ported from ``riglib.harness_skills.omp_agent_root`` (rig-cli) — this dispatcher
    cannot import rig-cli code, so the precedence logic is duplicated here. Keep the two
    in lockstep if omp's env-var contract ever changes. In particular, both env vars are
    expanded with ``$VAR``/``${VAR}`` substitution BEFORE ``~`` expansion (mirroring
    rig-cli's own ``expand_user_path``, which is ``expanduser(expandvars(path))``) — a value
    like ``PI_CODING_AGENT_DIR='$HOME/omp-agent'`` must resolve to the same real path rig
    installs the descriptor dir and extension symlink into, or this dispatcher would load
    zero descriptors from the wrong (unexpanded) path and silently allow everything.
    """
    agent_dir_override = os.environ.get("PI_CODING_AGENT_DIR")
    if agent_dir_override:
        p = Path(os.path.expanduser(os.path.expandvars(agent_dir_override)))
        if p.is_absolute():
            return str(p)
        return f"~/{agent_dir_override}"
    config_dir = os.environ.get("PI_CONFIG_DIR")
    if config_dir:
        p = Path(os.path.expanduser(os.path.expandvars(config_dir)))
        if p.is_absolute():
            return str(p / "agent")
        return f"~/{config_dir}/agent"
    return "~/.omp/agent"


def dispatch(event_name: str, omp_event: dict) -> dict | None:
    """Run applicable v1 hooks for this omp event."""
    tool = _tool_name(omp_event)
    point = point_for_event(event_name, tool)
    if point is None:
        return None
    runner = _load_runner()
    if runner is None:
        return None
    load_descriptors, run_hook = runner
    specs = load_descriptors(point, hooks_dir(), warn=_warn)
    if not specs:
        return None
    for spec in specs:
        for v1_event in _v1_events_for_dispatch(omp_event, point=point):
            outcome, reason = run_hook(spec, v1_event, warn=_warn)
            if outcome == "block":
                return omp_block_output(reason)
    return None


def _load_runner():
    try:
        from agent_hooks_v1 import load_descriptors, run_hook
    except Exception as exc:  # noqa: BLE001 - bridge-level failures fail open
        _warn(f"could not import shared v1 runner, failing OPEN (allowing the call): {exc}")
        return None
    return load_descriptors, run_hook


def main(argv: list[str]) -> int:
    """CLI entry: ``omp_hook_bridge <event-name>``.

    Top-level dispatcher failures fail open: a broken bridge must not wedge every omp tool
    call. Deliberate hook blocks still return block JSON on stdout. This is the SAME
    fail-open contract as the codex/opencode dispatchers' ``main`` — the divergence from
    them is entirely in ``extension.ts``, which (unlike opencode's plugin) never turns a
    bridge failure into a throw on the pre-execution path either. See the module and
    extension.ts docstrings for why.
    """
    event_name = argv[0] if argv else "tool_call"
    if event_name not in _KNOWN_EVENTS:
        _warn(f"unknown event arg {event_name!r}; stdin's event field will be used if present")
    try:
        raw = sys.stdin.read()
        omp_event = json.loads(raw) if raw.strip() else {}
        if not isinstance(omp_event, dict):
            omp_event = {}
        event_name = str(omp_event.get("event") or event_name)
        result = dispatch(event_name, omp_event)
    except Exception as exc:  # noqa: BLE001 - fail-open is the bridge contract
        _warn(f"dispatcher error, failing OPEN (allowing the call): {exc}")
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


def _tool_name(event: dict) -> str:
    return str(event.get("toolName") or "").lower()


def _tool_args(event: dict) -> dict:
    args = event.get("input")
    return dict(args) if isinstance(args, dict) else {}


def _tool_command(tool: str, args: dict) -> str:
    if tool in ("edit", "apply_patch"):
        return _edit_patch_text(tool, args)
    val = args.get("command")
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return " ".join(str(part) for part in val)
    return ""


def _edit_patch_text(tool: str, args: dict) -> str:
    """The raw patch/section text for `edit` (hashline) or `apply_patch` (classic)."""
    if tool == "edit":
        val = args.get("input")
        return val if isinstance(val, str) else ""
    for key in ("patch", "patchText", "input"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return ""


def _cwd(event: dict) -> str:
    val = event.get("cwd")
    if isinstance(val, str) and val:
        return val
    return os.getcwd()


def _event_id(event: dict) -> str:
    val = event.get("toolCallId")
    return str(val) if val else ""


def _normalize_task_args(args: dict) -> None:
    """Normalize omp's `task` tool arguments onto the shared pre-agent contract.

    omp's item shape is `{name?, agent?, task, effort?, outputSchema?, schemaMode?, isolated?}`
    (docs/tools/task.md) — not `subagent_type`/`prompt`/`run_in_background` like CC/opencode —
    and with `task.batch` on (the default) the wire shape is `{context, tasks: item[]}`. The
    pre-agent consumer (`agent-hooks/background-subagent-gate`) reads only the shared keys, so
    without this mapping EVERY omp dispatch looked non-trivial AND foreground and was blocked:

    - `agent` -> `subagent_type` (first item's `agent` for a batch).
    - `task` -> `prompt` (the batch joins every item's `task` with newlines — a multi-item
      batch is genuinely non-trivial and the gate's triviality check treats a newline as such).
    - `run_in_background` -> True. omp exposes NO per-call background lever: a non-blocking
      spawn becomes an `AsyncJobManager` background job by omp's own contract (sync only when
      the SESSION setting `async.enabled` is false or the agent's frontmatter says
      `blocking: true` — neither is a tool argument the model could ever set), so the gate's
      remediation ("dispatch it in the background") is un-followable from a tool call, exactly
      the situation the gate's own docstring already resolves for CC's `fork`/`isolation: remote`
      by trusting the harness contract. This trusts that contract as-is; an `async.enabled=false`
      session is treated the same, acceptable for an on_error=open discipline gate (see README).
    Raw `task`/`name`/`tasks`/`context` fields are kept alongside the normalized copies.
    """
    items = args.get("tasks")
    item_list = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
    if "subagent_type" not in args:
        agent = args.get("agent")
        if not isinstance(agent, str) and item_list:
            agent = item_list[0].get("agent")
        if isinstance(agent, str):
            args["subagent_type"] = agent
    if not isinstance(args.get("prompt"), str):
        texts = [it.get("task") for it in item_list if isinstance(it.get("task"), str)]
        if not texts and isinstance(args.get("task"), str):
            texts = [args["task"]]
        if texts:
            args["prompt"] = "\n".join(texts)
    if not isinstance(args.get("run_in_background"), bool) and (item_list or "task" in args):
        args["run_in_background"] = True


def _normalize_write_args(tool: str, raw_args: dict, args: dict, *, point: str) -> None:
    """Fill in `args.file_path`/`path`/`file_paths`(/`content`) for a write-shaped tool."""
    if tool == "write":
        path = raw_args.get("path")
        if isinstance(path, str) and path:
            args.setdefault("file_path", path)
            args.setdefault("path", path)
        if point == "pre-write" and not isinstance(args.get("content"), str):
            content = raw_args.get("content")
            args["content"] = content if isinstance(content, str) else ""
        return
    if tool == "edit":
        text = _edit_patch_text(tool, raw_args)
        paths = _hashline_file_paths(text)
        _apply_paths(args, paths)
        if point == "pre-write":
            args.setdefault("content", _hashline_added_content(text))
        return
    if tool == "apply_patch":
        text = _edit_patch_text(tool, raw_args)
        args.setdefault("patch", text)
        args.setdefault("patchText", text)
        paths = _patch_file_paths(text)
        _apply_paths(args, paths, move_target=_patch_move_target(text))
        if point == "pre-write":
            args["content"] = _patch_added_content(text)
        return
    # `notebook`: no confirmed omp tool schema for this today — best-effort passthrough of
    # whatever path-shaped field is present so a path-based hook is not blind to it.
    for key in ("path", "notebook_path", "file_path"):
        val = raw_args.get(key)
        if isinstance(val, str) and val:
            args.setdefault("notebook_path", val)
            args.setdefault("file_path", val)
            args.setdefault("path", val)
            break


def _apply_paths(args: dict, paths: list[str], *, move_target: str = "") -> None:
    if not paths:
        return
    args.setdefault("file_paths", paths)
    if len(paths) == 1:
        args.setdefault("file_path", paths[0])
        args.setdefault("path", paths[0])
    elif move_target:
        args.setdefault("file_path", move_target)
        args.setdefault("path", move_target)


def _hashline_file_paths(patch: str) -> list[str]:
    """Every `[PATH#TAG]` section path in a hashline payload, in order, de-duplicated."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        m = _HASHLINE_HEADER.match(line.strip())
        if m:
            path = m.group("path")
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _hashline_added_content(patch: str) -> str:
    """Best-effort concatenation of every `+`-prefixed body row across all sections.

    APPROXIMATION, documented in README.md: this does not perfectly reconstruct final
    per-file content when named registers or CUT-then-PUT paste are used across sections
    (a register paste carries no body row at all) — it only recovers the literal body rows
    a `PUT ...:` operation carries inline. Good enough for the consumer hooks' actual need
    (path-based gating), not a faithful hashline interpreter.
    """
    lines: list[str] = []
    in_body = False
    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if _HASHLINE_HEADER.match(stripped) or stripped in ("*** Begin Patch", "*** End Patch"):
            in_body = False
            continue
        if _HASHLINE_PUT_WITH_BODY.match(stripped):
            in_body = True
            continue
        if in_body and line.startswith("+"):
            lines.append(line[1:])
        else:
            in_body = False
    return "\n".join(lines)


def _v1_events_for_dispatch(omp_event: dict, *, point: str) -> list[dict]:
    """Fan out one v1 event per file path for a multi-file write-shaped call.

    Mirrors `opencode_hook_bridge.dispatch._v1_events_for_dispatch`: a path-based hook
    (worktree-only-writes) must see every touched path, not just the first.
    """
    base = to_v1_event(omp_event, point=point)
    args = base.get("args")
    if not isinstance(args, dict):
        return [base]
    file_paths = args.get("file_paths")
    if point not in ("pre-write", "post-write") or not isinstance(file_paths, list) or len(file_paths) <= 1:
        return [base]

    tool = base.get("tool")
    text = str(args.get("patch") or args.get("input") or "")
    content_by_path = (
        _patch_added_content_by_path(text) if tool == "apply_patch" else _hashline_content_by_path(text)
    )

    events: list[dict] = []
    for raw_path in file_paths:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        event = dict(base)
        event_args = dict(args)
        event_args["file_path"] = raw_path
        event_args["path"] = raw_path
        event_args.pop("file_paths", None)
        if point == "pre-write":
            event_args["content"] = content_by_path.get(raw_path, "")
        event["args"] = event_args
        events.append(event)
    return events or [base]


def _hashline_content_by_path(patch: str) -> dict[str, str]:
    """Per-path best-effort body content for a multi-section hashline payload.

    Same approximation caveats as `_hashline_added_content`; sections sharing a path
    accumulate their body rows in order.
    """
    content: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    in_body = False

    def finish() -> None:
        if current_path is not None:
            content[current_path] = "\n".join(current_lines)

    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        m = _HASHLINE_HEADER.match(stripped)
        if m:
            finish()
            current_path = m.group("path")
            current_lines = []
            in_body = False
            continue
        if stripped in ("*** Begin Patch", "*** End Patch"):
            in_body = False
            continue
        if _HASHLINE_PUT_WITH_BODY.match(stripped):
            in_body = True
            continue
        if in_body and line.startswith("+"):
            current_lines.append(line[1:])
        else:
            in_body = False
    finish()
    return content


# --- Classic apply_patch envelope parsing -----------------------------------------------
# Ported from `opencode_hook_bridge.dispatch` (`_patch_file_paths` / `_patch_added_content` /
# `_patch_added_content_by_path` / `_patch_move_target`) rather than re-derived: omp's
# documented `apply_patch` custom-tool wire mode uses the SAME classic `*** Update File: ` /
# `*** Add File: ` / `*** Delete File: ` / `*** Move to: ` envelope family opencode's
# `apply_patch` tool uses. Keep the two copies in lockstep if that envelope format changes;
# see README.md for why this bridge does not simply import the opencode module.


def _patch_file_paths(patch: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    pending_update: str | None = None

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            paths.append(path)

    def flush_update() -> None:
        nonlocal pending_update
        if pending_update:
            add(pending_update)
            pending_update = None

    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            flush_update()
            pending_update = line[len("*** Update File: ") :].strip()
        elif line.startswith("*** Move to: "):
            flush_update()
            pending_update = line[len("*** Move to: ") :].strip()
        elif line.startswith("*** Add File: "):
            flush_update()
            add(line[len("*** Add File: ") :].strip())
        elif line.startswith("*** Delete File: "):
            flush_update()
            add(line[len("*** Delete File: ") :].strip())
    flush_update()
    return paths


def _patch_added_content(patch: str) -> str:
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)


def _patch_added_content_by_path(patch: str) -> dict[str, str]:
    content: dict[str, str] = {}
    current_paths: list[str] = []
    current_lines: list[str] = []

    def finish() -> None:
        nonlocal current_paths, current_lines
        for path in current_paths:
            content[path] = "\n".join(current_lines)
        current_paths = []
        current_lines = []

    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            finish()
            current_paths = [line[len("*** Update File: ") :].strip()]
        elif line.startswith("*** Move to: "):
            finish()
            moved_to = line[len("*** Move to: ") :].strip()
            current_paths = [moved_to] if moved_to else []
        elif line.startswith("*** Add File: "):
            finish()
            current_paths = [line[len("*** Add File: ") :].strip()]
        elif line.startswith("*** Delete File: "):
            finish()
            current_paths = [line[len("*** Delete File: ") :].strip()]
        elif current_paths and line.startswith("+"):
            current_lines.append(line[1:])
    finish()
    return content


def _patch_move_target(patch: str) -> str:
    for line in patch.splitlines():
        if line.startswith("*** Move to: "):
            return line[len("*** Move to: ") :].strip()
    return ""


def _warn(msg: str) -> None:
    sys.stderr.write(f"omp-hook-bridge: {msg}\n")
