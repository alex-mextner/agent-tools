"""Clean-room proof for the agents-hooks/v1 -> opencode plugin bridge.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_opencode_hook_bridge.py -q

opencode loads JavaScript/TypeScript plugins from ``~/.config/opencode/plugins``.
The rig-provisioned plugin calls this Python dispatcher with opencode's
``tool.execute.before`` / ``tool.execute.after`` payloads so installed
``agents-hooks/v1`` descriptors can run for opencode too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import opencode_hook_bridge.dispatch as dispatch  # noqa: E402

BLOCK_RAW_PR_MERGE = (
    _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
)
BACKGROUND_SUBAGENT_GATE = (
    _REPO / "agent-hooks" / "background-subagent-gate" / "background_subagent_gate.py"
)


def _install_descriptor(
    hooks_dir: Path,
    *,
    hook_id: str,
    point: str,
    cmd: Path,
    on_error: str = "closed",
    priority: int = 10,
) -> Path:
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


def _run_dispatch(event: str, opencode_event: dict, *, hooks_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OPENCODE_HOOKS_DIR"] = str(hooks_dir)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "opencode_hook_bridge", event],
        input=json.dumps(opencode_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _recording_hook(tmp_path: Path, log_path: Path) -> Path:
    script = tmp_path / "record_hook.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        "import sys\n"
        f"pathlib.Path({str(log_path)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_point_for_event_maps_opencode_tools_to_v1_points():
    assert dispatch.point_for_event("tool.execute.before", "bash") == "pre-bash"
    assert dispatch.point_for_event("tool.execute.before", "edit") == "pre-write"
    assert dispatch.point_for_event("tool.execute.before", "write") == "pre-write"
    assert dispatch.point_for_event("tool.execute.before", "apply_patch") == "pre-write"
    assert dispatch.point_for_event("tool.execute.before", "task") == "pre-agent"

    assert dispatch.point_for_event("tool.execute.after", "edit") == "post-write"
    assert dispatch.point_for_event("tool.execute.after", "write") == "post-write"
    assert dispatch.point_for_event("tool.execute.after", "apply_patch") == "post-write"

    assert dispatch.point_for_event("tool.execute.before", "read") is None
    assert dispatch.point_for_event("tool.execute.after", "bash") is None
    assert dispatch.point_for_event("session.idle", None) is None


def test_to_v1_event_carries_bash_command_and_metadata():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "gh pr merge 42 --admin"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-bash")

    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["event_id"] == "ses_1"
    assert v1["tool"] == "bash"
    assert v1["point"] == "pre-bash"
    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["cwd"] == "/repo"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_drops_forged_agent_identity_from_tool_args():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "task"},
        "output": {
            "args": {
                "subagent_type": "general",
                "prompt": "inspect the entire repository and fix the issue",
                "description": "multi-step implementation",
                "agent_id": "forged",
            }
        },
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-agent")

    assert v1["point"] == "pre-agent"
    assert v1["args"]["prompt"] == "inspect the entire repository and fix the issue"
    assert v1["args"]["description"] == "multi-step implementation"
    assert v1["args"]["subagent_type"] == "general"
    assert "agent_id" not in v1["args"]


def test_to_v1_event_normalizes_apply_patch_paths_and_added_content():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/new.py\n"
        "+API_KEY = 'secret'\n"
        "*** Update File: src/existing.py\n"
        "+print('changed')\n"
        "*** End Patch\n"
    )
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "apply_patch", "sessionID": "ses_1"},
        "output": {"args": {"patchText": patch}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["command"] == patch
    assert v1["args"]["patch"] == patch
    assert v1["args"]["patchText"] == patch
    assert v1["args"]["file_paths"] == ["src/new.py", "src/existing.py"]
    assert "API_KEY = 'secret'" in v1["args"]["content"]


def test_v1_events_for_dispatch_splits_multi_file_apply_patch_content():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/a.py\n"
        "+print('a')\n"
        "*** Update File: src/b.py\n"
        "+print('b')\n"
        "*** End Patch\n"
    )
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "apply_patch", "sessionID": "ses_1"},
        "output": {"args": {"patchText": patch}},
    }

    events = dispatch._v1_events_for_dispatch(opencode_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["src/a.py", "src/b.py"]
    assert [e["args"]["content"] for e in events] == ["print('a')", "print('b')"]
    assert all("file_paths" not in e["args"] for e in events)


def test_to_v1_event_normalizes_simple_write_path_and_content():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "write", "sessionID": "ses_1"},
        "output": {"args": {"filePath": "src/app.py", "content": "print('ok')\n"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/app.py"
    assert v1["args"]["path"] == "src/app.py"
    assert v1["args"]["content"] == "print('ok')\n"


def test_opencode_bridge_blocks_raw_pr_merge_with_real_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="block-raw-pr-merge",
        point="pre-bash",
        cmd=BLOCK_RAW_PR_MERGE,
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "gh pr merge 42 --admin"}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "gh ship" in out["reason"]


def test_opencode_bridge_blocks_foreground_nontrivial_task(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="background-subagent-gate",
        point="pre-agent",
        cmd=BACKGROUND_SUBAGENT_GATE,
        on_error="open",
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "task", "sessionID": "ses_1"},
        "output": {
            "args": {
                "subagent_type": "general",
                "description": "implement the missing bridge and tests",
                "prompt": (
                    "Inspect the provisioning code, implement the bridge, run tests, and report.\n"
                    "Include evidence for Claude, Codex, and opencode, and do not mutate unrelated files."
                ),
            }
        },
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "BACKGROUND" in out["reason"]


def test_opencode_bridge_dispatches_post_write_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    log = tmp_path / "event.json"
    _install_descriptor(
        hooks_dir,
        hook_id="record-post-write",
        point="post-write",
        cmd=_recording_hook(tmp_path, log),
    )
    event = {
        "hook": "tool.execute.after",
        "cwd": str(tmp_path),
        "input": {"tool": "write", "sessionID": "ses_1"},
        "output": {"args": {"filePath": "src/app.py", "content": "print('ok')\n"}},
    }

    proc = _run_dispatch("tool.execute.after", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""
    recorded = json.loads(log.read_text(encoding="utf-8"))
    assert recorded["point"] == "post-write"
    assert recorded["args"]["file_path"] == "src/app.py"


def test_opencode_bridge_fail_open_for_open_descriptor_runtime_error(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="missing-open-hook",
        point="pre-bash",
        cmd=tmp_path / "missing-hook.py",
        on_error="open",
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "echo allowed"}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "on_error=open" in proc.stderr


def test_opencode_bridge_blocks_closed_descriptor_runtime_error(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="missing-closed-hook",
        point="pre-bash",
        cmd=tmp_path / "missing-hook.py",
        on_error="closed",
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "echo denied"}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "on_error=closed" in out["reason"]


def test_opencode_bridge_allows_when_hooks_dir_missing(tmp_path):
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "echo allowed"}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=tmp_path / "missing-hooks")

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_opencode_bridge_skips_malformed_descriptor_and_runs_valid_hook(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "bad.pre-bash.json").write_text("{not json", encoding="utf-8")
    log = tmp_path / "event.json"
    _install_descriptor(
        hooks_dir,
        hook_id="record-pre-bash",
        point="pre-bash",
        cmd=_recording_hook(tmp_path, log),
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "echo recorded"}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "bad.pre-bash.json" in proc.stderr
    recorded = json.loads(log.read_text(encoding="utf-8"))
    assert recorded["point"] == "pre-bash"
    assert recorded["command"] == "echo recorded"
