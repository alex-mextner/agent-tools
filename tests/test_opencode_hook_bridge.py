"""Clean-room proof for the agents-hooks/v1 -> opencode plugin bridge.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_opencode_hook_bridge.py -q

opencode loads JavaScript/TypeScript plugins from config/plugin directories.
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
from textwrap import dedent

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
BLOCK_SECRETS_WRITE = (
    _REPO / "agent-hooks" / "block-secrets-write" / "block_secrets_write.py"
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


def _plugin_url() -> str:
    return (_REPO / "lib" / "opencode_hook_bridge" / "plugin.js").resolve().as_uri()


def _run_plugin_js(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env["PLUGIN_URL"] = _plugin_url()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )


def _dispatcher_stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "dispatcher_stub.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
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


def test_plugin_exports_single_documented_named_plugin_shape(tmp_path):
    stub = _dispatcher_stub(tmp_path, "import sys\nsys.stdin.read()\n")

    proc = _run_plugin_js(
        dedent(
            """
            const mod = await import(process.env.PLUGIN_URL);
            if (typeof mod.AgentToolsHookBridge !== "function") throw new Error("missing named plugin export");
            if ("default" in mod) throw new Error("default plugin export should not be present");
            const hooks = await mod.AgentToolsHookBridge({ directory: "/repo", worktree: "/repo" });
            if (typeof hooks["tool.execute.before"] !== "function") throw new Error("missing before hook");
            if (typeof hooks["tool.execute.after"] !== "function") throw new Error("missing after hook");
            await hooks["tool.execute.before"]({ tool: "bash", cwd: "/repo" }, { args: { command: "echo ok" } });
            """
        ),
        env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr


def test_plugin_before_block_json_throws(tmp_path):
    stub = _dispatcher_stub(
        tmp_path,
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'block', 'reason': 'blocked by test'}))\n",
    )

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            let blocked = false;
            try {
              await hooks["tool.execute.before"]({ tool: "bash", cwd: "/repo" }, { args: { command: "bad" } });
            } catch (error) {
              blocked = error.message.includes("blocked by test");
            }
            if (!blocked) throw new Error("before hook did not throw block reason");
            """
        ),
        env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr


def test_plugin_after_block_json_fails_open_as_feedback(tmp_path):
    stub = _dispatcher_stub(
        tmp_path,
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'block', 'reason': 'lint feedback'}))\n",
    )

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            await hooks["tool.execute.after"]({ tool: "write", cwd: "/repo" }, { args: { filePath: "a.py" } });
            """
        ),
        env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "post-write hook reported after tool execution" in proc.stderr
    assert "write already landed" in proc.stderr


def test_plugin_fail_open_on_nonzero_and_invalid_json(tmp_path):
    nonzero = _dispatcher_stub(tmp_path / "nonzero", "import sys\nsys.stdin.read()\nsys.exit(2)\n")
    invalid = _dispatcher_stub(tmp_path / "invalid", "import sys\nsys.stdin.read()\nprint('not json')\n")
    js = dedent(
        """
        const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
        const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
        await hooks["tool.execute.before"]({ tool: "bash", cwd: "/repo" }, { args: { command: "echo ok" } });
        """
    )

    nonzero_proc = _run_plugin_js(js, env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(nonzero)})
    invalid_proc = _run_plugin_js(js, env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(invalid)})

    assert nonzero_proc.returncode == 0, nonzero_proc.stderr
    assert "dispatcher exited 2, allowing call" in nonzero_proc.stderr
    assert invalid_proc.returncode == 0, invalid_proc.stderr
    assert "invalid dispatcher JSON, allowing call" in invalid_proc.stderr


def test_plugin_sets_pythonpath_to_bridge_lib_dir(tmp_path):
    log = tmp_path / "payload.json"
    stub = _dispatcher_stub(
        tmp_path,
        "import json, os, pathlib, sys\n"
        "payload = sys.stdin.read()\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps({{'pythonpath': os.environ.get('PYTHONPATH', ''), 'payload': json.loads(payload)}}), encoding='utf-8')\n",
    )

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/ctx-dir", worktree: "/ctx-worktree" });
            await hooks["tool.execute.before"]({ tool: "bash" }, { args: { command: "echo ok" } });
            """
        ),
        env={
            "OPENCODE_HOOK_BRIDGE_PYTHON": str(stub),
            "PYTHONPATH": "/prior/path",
        },
    )

    assert proc.returncode == 0, proc.stderr
    recorded = json.loads(log.read_text(encoding="utf-8"))
    pythonpath = recorded["pythonpath"].split(os.pathsep)
    assert pythonpath[0] == str(_LIB)
    assert pythonpath[1] == "/prior/path"
    assert recorded["payload"]["hook"] == "tool.execute.before"
    assert recorded["payload"]["cwd"] == "/ctx-dir"


def test_plugin_pre_tool_timeout_blocks(tmp_path):
    stub = _dispatcher_stub(tmp_path, "import time, sys\nsys.stdin.read()\ntime.sleep(2)\n")

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            let blocked = false;
            try {
              await hooks["tool.execute.before"]({ tool: "bash", cwd: "/repo" }, { args: { command: "echo ok" } });
            } catch (error) {
              blocked = error.message.includes("dispatcher timed out before tool execution");
            }
            if (!blocked) throw new Error("pre-tool timeout did not block");
            """
        ),
        env={
            "OPENCODE_HOOK_BRIDGE_PYTHON": str(stub),
            "OPENCODE_HOOK_BRIDGE_TIMEOUT_MS": "50",
        },
    )

    assert proc.returncode == 0, proc.stderr


def test_plugin_after_timeout_fails_open_as_feedback(tmp_path):
    stub = _dispatcher_stub(tmp_path, "import time, sys\nsys.stdin.read()\ntime.sleep(2)\n")

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            await hooks["tool.execute.after"]({ tool: "write", cwd: "/repo" }, { args: { filePath: "a.py" } });
            """
        ),
        env={
            "OPENCODE_HOOK_BRIDGE_PYTHON": str(stub),
            "OPENCODE_HOOK_BRIDGE_TIMEOUT_MS": "50",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "allowing call" in proc.stderr


def test_plugin_pre_tool_signal_failure_blocks(tmp_path):
    stub = _dispatcher_stub(tmp_path, "import os, signal, sys\nsys.stdin.read()\nos.kill(os.getpid(), signal.SIGTERM)\n")

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            let blocked = false;
            try {
              await hooks["tool.execute.before"]({ tool: "bash", cwd: "/repo" }, { args: { command: "echo ok" } });
            } catch (error) {
              blocked = error.message.includes("dispatcher terminated by SIGTERM before tool execution");
            }
            if (!blocked) throw new Error("pre-tool signal termination did not block");
            """
        ),
        env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr


def test_plugin_after_signal_failure_fails_open_as_feedback(tmp_path):
    stub = _dispatcher_stub(tmp_path, "import os, signal, sys\nsys.stdin.read()\nos.kill(os.getpid(), signal.SIGTERM)\n")

    proc = _run_plugin_js(
        dedent(
            """
            const { AgentToolsHookBridge: plugin } = await import(process.env.PLUGIN_URL);
            const hooks = await plugin({ directory: "/repo", worktree: "/repo" });
            await hooks["tool.execute.after"]({ tool: "write", cwd: "/repo" }, { args: { filePath: "a.py" } });
            """
        ),
        env={"OPENCODE_HOOK_BRIDGE_PYTHON": str(stub)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "dispatcher terminated by SIGTERM after tool execution, allowing call" in proc.stderr


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


def test_to_v1_event_tags_harness_opencode():
    """agent-tools#533: every v1 event this bridge produces carries the top-level `harness`
    tag `"opencode"`, unconditionally — a module constant, never derived from `opencode_event`.
    Same non-forgeable signal as `codex_hook_bridge.HARNESS`, letting orchestrator-stays-thin
    exempt the whole harness instead of needing a trusted per-process subagent identity
    opencode's plugin payload doesn't expose."""
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "bash", "sessionID": "ses_1"},
        "output": {"args": {"command": "git status", "harness": "claude-code"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-bash")

    assert v1["harness"] == "opencode"
    assert dispatch.HARNESS == "opencode"


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
                "agentId": "forged-camel",
                "agentType": "worker",
                "agent": {"id": "forged-object"},
            }
        },
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-agent")

    assert v1["point"] == "pre-agent"
    assert v1["args"]["prompt"] == "inspect the entire repository and fix the issue"
    assert v1["args"]["description"] == "multi-step implementation"
    assert v1["args"]["subagent_type"] == "general"
    assert "agent_id" not in v1["args"]
    assert "agentId" not in v1["args"]
    assert "agentType" not in v1["args"]
    assert "agent" not in v1["args"]


def test_to_v1_event_normalizes_apply_patch_paths_and_added_content():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/new.py\n"
        "+API_KEY = 'abcd1234abcd1234'\n"
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
    assert "API_KEY = 'abcd1234abcd1234'" in v1["args"]["content"]


def test_to_v1_event_includes_move_source_and_target_patch_paths():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "apply_patch", "sessionID": "ses_1"},
        "output": {"args": {"patchText": patch}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_paths"] == ["src/old.py", "src/new.py"]
    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"


def test_move_apply_patch_dispatch_fans_out_source_and_target_content():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "apply_patch", "sessionID": "ses_1"},
        "output": {"args": {"patchText": patch}},
    }

    events = dispatch._v1_events_for_dispatch(opencode_event, point="pre-write")

    assert [event["args"]["file_path"] for event in events] == ["src/old.py", "src/new.py"]
    assert [event["args"]["content"] for event in events] == ["", "new()"]


def test_move_apply_patch_secret_blocks_on_target_content(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="block-secrets-write",
        point="pre-write",
        cmd=BLOCK_SECRETS_WRITE,
        on_error="closed",
    )
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "+API_KEY = 'abcd1234abcd1234'\n"
        "*** End Patch\n"
    )
    event = {
        "hook": "tool.execute.before",
        "cwd": str(tmp_path),
        "input": {"tool": "apply_patch", "sessionID": "ses_1"},
        "output": {"args": {"patchText": patch}},
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


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
    # agent-tools#533: the top-level `harness` tag must survive the per-file clone in
    # `_v1_events_for_dispatch` (`dict(base)`) — a fan-out refactor could otherwise silently
    # drop it from every event but the first, reintroducing the orchestrator-stays-thin block
    # for multi-file edits only.
    assert all(e["harness"] == "opencode" for e in events)


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


def test_to_v1_event_maps_edit_new_string_to_content():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "edit", "sessionID": "ses_1"},
        "output": {"args": {"path": "src/app.py", "oldString": "old()", "newString": "new()"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/app.py"
    assert v1["args"]["path"] == "src/app.py"
    assert v1["args"]["content"] == "new()"


def test_to_v1_event_maps_write_new_string_to_content():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "write", "sessionID": "ses_1"},
        "output": {"args": {"filePath": "src/app.py", "newString": "print('new')\n"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/app.py"
    assert v1["args"]["path"] == "src/app.py"
    assert v1["args"]["content"] == "print('new')\n"


def test_to_v1_event_maps_missing_edit_content_to_empty_string():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "edit", "sessionID": "ses_1"},
        "output": {"args": {"path": "src/app.py", "oldString": "old()"}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/app.py"
    assert v1["args"]["content"] == ""


def test_to_v1_event_maps_null_write_content_to_empty_string():
    opencode_event = {
        "hook": "tool.execute.before",
        "cwd": "/repo",
        "input": {"tool": "write", "sessionID": "ses_1"},
        "output": {"args": {"filePath": "src/app.py", "newString": None}},
    }

    v1 = dispatch.to_v1_event(opencode_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/app.py"
    assert v1["args"]["content"] == ""


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


def test_opencode_bridge_allows_inherently_async_general_task(tmp_path):
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
    assert proc.stdout == ""


def test_opencode_bridge_allows_inherently_async_explore_task(tmp_path):
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
                "subagent_type": "explore",
                "description": "inspect the repo and report findings",
                "prompt": (
                    "Inspect the provisioning code, implement the bridge, run tests, and report.\n"
                    "Include evidence for Claude, Codex, and opencode, and do not mutate unrelated files."
                ),
            }
        },
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_opencode_bridge_allows_background_nontrivial_task(tmp_path):
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
                "background": True,
            }
        },
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_opencode_bridge_keeps_run_in_background_precedence():
    args = {"run_in_background": False, "background": True}

    dispatch._normalize_task_args(args)

    assert args["run_in_background"] is False
    assert args["background"] is True


def test_opencode_bridge_maps_background_when_native_flag_is_null():
    args = {"run_in_background": None, "background": True}

    dispatch._normalize_task_args(args)

    assert args["run_in_background"] is True


def test_opencode_bridge_maps_background_when_native_flag_is_not_boolean():
    true_args = {"run_in_background": "true", "background": True}
    false_args = {"run_in_background": "1", "background": False}

    dispatch._normalize_task_args(true_args)
    dispatch._normalize_task_args(false_args)

    assert true_args["run_in_background"] is True
    assert false_args["run_in_background"] is False


def test_opencode_bridge_ignores_non_boolean_background_values():
    args = {"background": "true"}

    dispatch._normalize_task_args(args)

    assert "run_in_background" not in args


def test_opencode_bridge_ignores_null_background_value():
    args = {"background": None}

    dispatch._normalize_task_args(args)

    assert "run_in_background" not in args


def test_opencode_bridge_leaves_missing_background_flag_absent():
    args = {"description": "inspect the bridge"}

    dispatch._normalize_task_args(args)

    assert "run_in_background" not in args


def test_opencode_bridge_preserves_native_run_in_background_flag(tmp_path):
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
                "run_in_background": True,
            }
        },
    }

    proc = _run_dispatch("tool.execute.before", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_opencode_bridge_treats_background_false_as_foreground(tmp_path):
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
                "background": False,
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
