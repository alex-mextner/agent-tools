"""Clean-room proof that the agents-hooks/v1 → Claude Code bridge actually BLOCKS.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_cc_hook_bridge.py -q

The whole point of issue #18: agent-hook descriptors in ``~/.claude/hooks/*.json`` are
INERT in Claude Code (CC only runs ``settings.json`` PreToolUse/PostToolUse/Stop hooks).
This suite drives the bridge dispatcher in an isolated ``$HOME``, installs a real
agent-hook descriptor + script, feeds the dispatcher a CC tool-call event that the guard
should block, and asserts the dispatcher emits the CC block signal we confirmed against
the live docs (https://code.claude.com/docs/en/hooks, CC 2.1.177):

  - PreToolUse block  → exit 0 + ``hookSpecificOutput.permissionDecision == "deny"``
  - Stop block        → exit 0 + top-level ``decision == "block"``
  - benign call       → no deny (the tool proceeds through normal permission flow)

Every case runs the dispatcher as a SUBPROCESS (exactly how CC invokes it), so the test
exercises the real entrypoint + the real agent-hook scripts, not a mock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cc_hook_bridge.dispatch as dispatch  # noqa: E402

# The shipped guard the orchestrator named: block-raw-pr-merge.
BLOCK_RAW_PR_MERGE = (
    _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
)
# The pre-agent guard: background-subagent-gate (blocks a foreground non-trivial dispatch).
BACKGROUND_SUBAGENT_GATE = (
    _REPO / "agent-hooks" / "background-subagent-gate" / "background_subagent_gate.py"
)


def _install_descriptor(hooks_dir: Path, *, hook_id: str, point: str, cmd: Path,
                        on_error: str = "closed", priority: int = 10) -> Path:
    """Write a `<id>.<point>.json` descriptor with an absolute `cmd`, as rig installs it."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "id": hook_id,
        "point": point,
        "cmd": str(cmd),
        "priority": priority,
        "timeout_ms": 3000,
        "on_error": on_error,
        "description": f"test descriptor for {hook_id}",
    }
    path = hooks_dir / f"{hook_id}.{point}.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _run_dispatch(event: str, cc_event: dict, *, home: Path) -> subprocess.CompletedProcess:
    """Invoke the dispatcher exactly as CC's settings.json would: `python -m cc_hook_bridge <event>`."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cc_hook_bridge", event],
        input=json.dumps(cc_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ── pure mapping unit tests (fast, no subprocess) ─────────────────────────────────────

def test_point_for_event_maps_tool_to_logical_point():
    assert dispatch.point_for_event("PreToolUse", "Bash") == "pre-bash"
    assert dispatch.point_for_event("PreToolUse", "Write") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "Edit") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "MultiEdit") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "NotebookEdit") == "pre-write"
    # the subagent-dispatch tools map to the new pre-agent point (CC calls them Agent/Task)
    assert dispatch.point_for_event("PreToolUse", "Agent") == "pre-agent"
    assert dispatch.point_for_event("PreToolUse", "Task") == "pre-agent"
    assert dispatch.point_for_event("Stop", None) == "stop"
    # an unmapped tool (e.g. Read) on PreToolUse has no logical point → nothing fires
    assert dispatch.point_for_event("PreToolUse", "Read") is None


def test_to_v1_event_forwards_agent_id_and_type_for_pre_agent():
    """The subagent signal (agent_id/agent_type) must be surfaced under args so a
    subagent-exempt gate can tell a subagent's tool use apart from the main thread's."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "run_in_background": False},
          "agent_id": "sub-42", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert v1["point"] == "pre-agent"
    assert v1["args"]["agent_id"] == "sub-42"
    assert v1["args"]["agent_type"] == "general-purpose"
    # the dispatch payload (incl. run_in_background) rides along in args from tool_input
    assert v1["args"]["run_in_background"] is False
    assert v1["args"]["prompt"] == "do the thing"


def test_to_v1_event_main_thread_has_no_agent_id():
    """A main-thread Agent dispatch carries NO agent_id (the signal is absent) → the gate
    treats it as the orchestrator, not a subagent."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_top_level_agent_id_overrides_tool_input():
    """CC's TOP-LEVEL agent_id is authoritative: a (possibly stale/forged) tool_input.agent_id
    must be OVERWRITTEN by the real top-level value, not win over it (T2)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "agent_id": "forged", "agent_type": "x"},
          "agent_id": "real-sub", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert v1["args"]["agent_id"] == "real-sub"
    assert v1["args"]["agent_type"] == "general-purpose"


def test_to_v1_event_forged_tool_input_agent_id_does_not_exempt():
    """A forged tool_input.agent_id with NO top-level signal must be DROPPED — a main-thread
    dispatch that injects agent_id into tool_input must NOT exempt itself (T2)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "agent_id": "forged", "agent_type": "forged-t"},
          "cwd": "/repo"}  # ← no top-level agent_id/agent_type
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


def test_to_v1_event_forwards_agent_id_for_pre_bash():
    """The subagent signal must ride through for pre-bash too, not only pre-agent — so
    no-long-inline-process / orchestrator-stays-thin receive it for a subagent's Bash (T5)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"},
          "agent_id": "sub-7", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["args"]["agent_id"] == "sub-7"
    assert v1["args"]["agent_type"] == "general-purpose"


def test_to_v1_event_forged_tool_input_agent_id_dropped_for_pre_bash():
    """The same forged-signal protection applies to pre-bash: a forged tool_input.agent_id on a
    main-thread Bash (no top-level signal) must NOT exempt it from the inline-process gate."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "review", "agent_id": "forged"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_forged_tool_input_agent_id_dropped_for_pre_write():
    """The forged-signal protection must also hold for pre-WRITE: an Edit carrying
    `agent_id:"forged"` in tool_input with NO top-level signal must have it DROPPED, so
    orchestrator-stays-thin still judges it as a main-thread code write (#7)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
          "tool_input": {"file_path": "/repo/src/a.ts", "old_string": "a",
                         "new_string": "b", "agent_id": "forged", "agent_type": "forged-t"},
          "cwd": "/repo"}  # ← no top-level agent_id/agent_type
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


def test_to_v1_event_emits_the_point_for_each_logical_point():
    """`point` must round-trip into the v1 event for every point. orchestrator-stays-thin reads
    `event.get("point")`; if the bridge ever stopped setting it the hook would silently
    downgrade to allow. Lock it for pre-bash, pre-write, and pre-agent (#10)."""
    cases = [
        ("pre-bash", "Bash", {"command": "npm test"}),
        ("pre-write", "Write", {"file_path": "/repo/x.py", "content": "x"}),
        ("pre-agent", "Agent", {"prompt": "do the thing"}),
    ]
    for point, tool, tool_input in cases:
        cc = {"hook_event_name": "PreToolUse", "tool_name": tool,
              "tool_input": tool_input, "cwd": "/repo"}
        v1 = dispatch.to_v1_event(cc, point=point)
        assert v1["point"] == point


def test_to_v1_event_carries_the_bash_command():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "gh pr merge 5 --admin"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["point"] == "pre-bash"
    assert v1["tool"] == "Bash"
    assert v1["args"]["command"] == "gh pr merge 5 --admin"
    assert v1["cwd"] == "/repo"


def test_to_v1_event_carries_write_path_and_content():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/repo/x.py", "content": "API_KEY=..."}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["file_path"] == "/repo/x.py"
    assert v1["args"]["content"] == "API_KEY=..."
    # non-Bash tool → the top-level `command` fallback is empty (locks the bash-only contract)
    assert v1["command"] == ""


def test_to_v1_event_normalizes_multiedit_into_content():
    """A MultiEdit's edits[].new_string must surface in args.content so flat-content
    pre-write hooks (block-secrets-write) can scan it — else a secret slips through."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit",
          "tool_input": {"file_path": "/repo/x.py",
                         "edits": [{"old_string": "a", "new_string": "TOKEN=sk-live-xxxx"},
                                   {"old_string": "b", "new_string": "harmless"}]}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "TOKEN=sk-live-xxxx" in v1["args"]["content"]
    assert "harmless" in v1["args"]["content"]


def test_to_v1_event_normalizes_notebook_new_source():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": "/n.ipynb", "new_source": "SECRET=abc123"}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "SECRET=abc123" in v1["args"]["content"]
    # The NotebookEdit `notebook_path` must alias onto the standard path fields the shipped
    # pre-write hooks scope on — else path-scoped raw-env/secret-scan checks see an empty path
    # and wave the write through (guard bypass).
    assert v1["args"]["file_path"] == "/n.ipynb"
    assert v1["args"]["path"] == "/n.ipynb"


def test_to_v1_event_does_not_clobber_explicit_path():
    """A tool that already provides file_path keeps it; the notebook alias only FILLS a gap."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/real.py", "notebook_path": "/wrong.ipynb", "content": "x"}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["file_path"] == "/real.py"


def test_to_v1_event_preserves_explicit_content_over_normalization():
    """When the tool already provides a string `content` (Write), keep it verbatim."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/x", "content": "exact", "edits": [{"new_string": "other"}]}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["content"] == "exact"


def test_cc_block_output_pretooluse_is_permission_deny():
    out = dispatch.cc_block_output("PreToolUse", "nope")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_cc_block_output_stop_is_decision_block():
    out = dispatch.cc_block_output("Stop", "stay")
    assert out["decision"] == "block"
    assert out["reason"] == "stay"


# ── clean-room subprocess proof: a guard BLOCKS, a benign call PASSES ──────────────────

def test_real_guard_blocks_raw_pr_merge(tmp_path):
    """block-raw-pr-merge installed as a CC PreToolUse hook actually DENIES a raw merge."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")

    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 42 --admin"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "gh pr merge" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ship" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_real_guard_passes_benign_bash(tmp_path):
    """The same guard installed, but a benign `gh ship` is NOT blocked."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")

    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh ship 42"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)

    assert proc.returncode == 0, proc.stderr
    # benign → either empty stdout (allow) or a decision that is NOT deny
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision != "deny", out


# ── clean-room proof: the pre-agent point routes + the subagent signal is honored ─────────

def test_pre_agent_foreground_dispatch_is_blocked(tmp_path):
    """An `Agent` PreToolUse with no run_in_background routes to a pre-agent descriptor and
    BLOCKS (the orchestrator must dispatch non-trivial subagents in the background)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        # a clearly non-trivial single-line prompt (> 200 chars) run in the FOREGROUND
        "tool_input": {"prompt": "implement the feature: " + "x" * 220},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "BACKGROUND" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_agent_background_dispatch_passes(tmp_path):
    """The same gate, but a `run_in_background: true` dispatch is NOT blocked."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": "implement the feature: " + "x" * 220, "run_in_background": True},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_pre_agent_subagent_signal_forwarded_exempts(tmp_path):
    """A foreground dispatch made INSIDE a subagent (agent_id present) must be ALLOWED — proving
    the bridge forwards agent_id and the subagent-exempt gate reads it."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "implement the feature: " + "x" * 220},
        "agent_id": "sub-99",  # ← the subagent signal CC adds inside a dispatched agent
        "agent_type": "general-purpose",
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_forged_tool_input_agent_id_does_not_exempt_clean_room(tmp_path):
    """End-to-end (T2): a main-thread foreground dispatch that injects `agent_id` INTO
    tool_input — with NO top-level CC agent signal — must STILL be BLOCKED. The bridge drops
    the forged signal, so the subagent-exempt gate does not let the orchestrator off the hook."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        # forged agent_id inside tool_input; CC top-level carries NO agent_id (main thread)
        "tool_input": {"prompt": "implement the feature: " + "x" * 220, "agent_id": "forged"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_unmatched_tool_does_not_run_pre_bash_hook(tmp_path):
    """A pre-bash hook must NOT fire on a Read (no logical point) — selection by point."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Read",
                "tool_input": {"file_path": "/x"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_no_descriptors_is_allow(tmp_path):
    """Empty hooks dir (or none) → dispatcher is a clean no-op (fail-open)."""
    home = tmp_path / "home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "gh pr merge 1 --admin"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    # nothing installed → no deny
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_fail_open_on_garbage_stdin(tmp_path):
    """A broken/garbage stdin must NOT wedge every tool call — dispatcher fails OPEN."""
    home = tmp_path / "home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "cc_hook_bridge", "PreToolUse"],
        input="this is not json{{{",
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0
    # no deny emitted
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_hook_error_fail_closed_blocks(tmp_path):
    """A hook that ERRORS (non-0/non-10 exit) with on_error=closed must DENY the call."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    # a script that always crashes (exit 1) — simulates a broken security gate
    crasher = tmp_path / "crasher.py"
    crasher.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    crasher.chmod(0o755)
    _install_descriptor(hooks, hook_id="crasher", point="pre-bash",
                        cmd=crasher, on_error="closed")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_bad_timeout_ms_on_closed_gate_blocks(tmp_path):
    """A fail-closed gate with a non-numeric timeout_ms must DENY (descriptor error), not be
    silently skipped by the dispatcher's fail-open."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "broken-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": "5s", "on_error": "closed"}
    (hooks / "broken-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_non_utf8_block_output_on_OPEN_gate_still_blocks(tmp_path):
    """A hook that emits NON-UTF-8 bytes AND a deliberate block (exit 10) must BLOCK even on a
    fail-OPEN gate: the decode must not raise (errors='replace') and turn a real block into a
    hook error → on_error=open → allow. on_error='open' isolates this from the fail-closed
    path (where any error denies anyway). Regression: #18 / non-UTF-8 fail-open."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    blocker = tmp_path / "binary_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
        "sys.stdout.flush()\n"
        "sys.exit(10)\n"
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks, hook_id="binblk", point="pre-bash", cmd=blocker, on_error="open")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_negative_timeout_ms_on_closed_gate_blocks(tmp_path):
    """A NEGATIVE timeout_ms is a descriptor typo (not 'unset') → a fail-closed gate must DENY,
    consistent with how a non-numeric timeout_ms is handled."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "neg-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": -100, "on_error": "closed"}
    (hooks / "neg-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


@pytest.mark.parametrize("bad", [True, False])
def test_bool_timeout_ms_on_closed_gate_blocks(tmp_path, bad):
    """A BOOLEAN timeout_ms is a descriptor typo, not a number — bool is an int subclass, so
    int(True)==1 / int(False)==0 would otherwise sneak past as a 1 ms / 'unset' timeout. A
    fail-closed gate must DENY, like any other bad descriptor field."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "bool-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": bad, "on_error": "closed"}
    (hooks / "bool-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def _benign_exit0(tmp_path: Path) -> Path:
    script = tmp_path / "benign.py"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("timeout_ms", [0, None])
def test_zero_or_null_timeout_ms_uses_default_not_a_spurious_timeout(tmp_path, timeout_ms):
    """timeout_ms of 0 (or null) means 'unset' → the default, NOT a 1 ms floor that would make
    every such hook spuriously time out and, on a fail-closed gate, block a benign call."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "fast", "point": "pre-bash", "cmd": str(_benign_exit0(tmp_path)),
            "priority": 10, "timeout_ms": timeout_ms, "on_error": "closed"}
    (hooks / "fast.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    assert "deny" not in proc.stdout  # benign call allowed, not a spurious timeout-block


def test_bad_priority_does_not_crash_dispatch(tmp_path):
    """A non-numeric priority must NOT bubble to the top-level fail-open and skip OTHER hooks
    (including a fail-closed gate that should block)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    # one descriptor has a garbage priority; the real guard (good priority) must still block.
    bad = {"id": "bad-prio", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
           "priority": "high", "on_error": "open"}
    (hooks / "bad-prio.pre-bash.json").write_text(json.dumps(bad))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "gh pr merge 9 --admin"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_hook_error_fail_open_allows(tmp_path):
    """A hook that ERRORS with on_error=open must NOT block (advisory hook stays out of the way)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    crasher = tmp_path / "crasher.py"
    crasher.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    crasher.chmod(0o755)
    _install_descriptor(hooks, hook_id="crasher", point="pre-bash",
                        cmd=crasher, on_error="open")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_stop_block_emits_decision_block(tmp_path):
    """A stop hook that exit-10 blocks → CC top-level decision:block (different from PreToolUse)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    # a minimal stop hook that always blocks once with exit 10
    blocker = tmp_path / "stopper.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "sys.stdout.write(json.dumps({'hook_api':'agents-hooks/v1','decision':'block','message':'finish the checklist'}))\n"
        "sys.exit(10)\n"
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks, hook_id="stopper", point="stop",
                        cmd=blocker, on_error="open")
    cc_event = {"hook_event_name": "Stop", "stop_hook_active": True, "cwd": str(tmp_path)}
    proc = _run_dispatch("Stop", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "checklist" in out["reason"]


def test_first_block_wins_collects_reason(tmp_path):
    """Two pre-bash guards, both would block → first (by priority) wins, its reason surfaces."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    mk = tmp_path / "mk.py"
    mk.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "tag=sys.argv[1]\n"
        "sys.stdout.write(json.dumps({'hook_api':'agents-hooks/v1','decision':'block','message':tag}))\n"
        "sys.exit(10)\n"
    )
    mk.chmod(0o755)
    # priority 5 runs before priority 50 → "first" reason wins
    d1 = _install_descriptor(hooks, hook_id="first", point="pre-bash", cmd=mk, priority=5)
    d2 = _install_descriptor(hooks, hook_id="second", point="pre-bash", cmd=mk, priority=50)
    # patch args so each prints its own tag
    for path, tag in ((d1, "first-reason"), (d2, "second-reason")):
        spec = json.loads(path.read_text())
        spec["args"] = [tag]
        path.write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "anything"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "first-reason" in reason, out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
