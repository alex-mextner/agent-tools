"""The tg-ctl Stop-hook inbox (agent-tools#526 / tg-cli#306).

Two layers:
  - the shared KEY contract — the vectors here are byte-identical to tg-cli's
    ``tests/ctl-unreachable.test.ts`` (both sides derive the same inbox directory or the
    channel silently splits);
  - the consuming reader — pending → block reason + archive (at-most-once), and the
    fail-open cases: empty, missing, malformed, unwritable archive.
The last tests run the real ``cc_hook_bridge`` / ``codex_hook_bridge`` dispatchers as a
SUBPROCESS (how the harness invokes them) against a temp ``TG_CTL_CONFIG_DIR``.
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

from agenttools_tg_inbox import (  # noqa: E402
    agent_key,
    agent_key_for_process,
    consume_pending,
    format_block_reason,
    inbox_dir,
    parse_agent_name,
    sanitize_agent_name,
)
from agenttools_tg_inbox import core as inbox_core  # noqa: E402


def _entry(i: int, wrapped: str) -> str:
    return json.dumps({"id": i, "ts": "2026-09-06T00:00:00Z", "from": "Alex", "text": wrapped, "wrapped": wrapped})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_CTL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return tmp_path


# --- the shared key contract (mirror of ctl-unreachable.test.ts) ---


def test_agent_key_name_wins_and_is_sanitized():
    assert agent_key("landing", "/x") == "landing"
    assert agent_key("my agent/1", "/x") == "my_agent_1"
    assert agent_key("rig-fable.v2", "/x") == "rig-fable.v2"
    assert agent_key("x" * 100, "/x") == "x" * 64


def test_agent_key_cwd_hash_normalizes_trailing_slash():
    assert agent_key(None, "/Users/ultra/work/landing") == "cwd-ccfe64bae2f277d7"
    assert agent_key(None, "/Users/ultra/work/landing/") == "cwd-ccfe64bae2f277d7"
    assert agent_key("", "/") == "cwd-8a5edab282632443"


def test_sanitize_agent_name_empty_and_dots_only_are_none():
    assert sanitize_agent_name("") is None
    assert sanitize_agent_name(".") is None
    assert sanitize_agent_name("..") is None  # never a path escape out of inbox/
    assert sanitize_agent_name("ok-1") == "ok-1"
    assert agent_key("..", "/") == "cwd-8a5edab282632443"
    assert inbox_dir(agent_key("..", "/")).parent.name == "inbox"


def test_parse_agent_name_multi_word_yields_first_word_like_the_ts_side():
    # ps flattens argv: `--name "my agent/1"` reads as `--name my agent/1` on both sides.
    assert parse_agent_name("claude --name my agent/1") == "my"


def test_parse_agent_name_forms():
    assert parse_agent_name("claude --permission-mode bypassPermissions --name rig-fable") == "rig-fable"
    assert parse_agent_name("claude --name=landing --resume") == "landing"
    assert parse_agent_name("claude --name --resume") is None
    assert parse_agent_name("claude") is None


def test_agent_key_for_process_uses_claude_pid_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setattr(inbox_core, "_ps_line", lambda pid: (1, "claude --name landing") if pid == 4242 else None)
    assert agent_key_for_process("/x") == "landing"


def test_agent_key_for_process_climbs_ancestry_to_the_agent(monkeypatch):
    table = {
        10: (9, "/bin/sh -c PYTHONPATH=/x python3 -m cc_hook_bridge Stop"),
        9: (8, "/Users/u/.local/bin/claude --name hooked"),
        8: (1, "-zsh"),
    }
    monkeypatch.setattr(inbox_core, "_ps_line", lambda pid: table.get(pid))
    assert agent_key_for_process("/x", start_pid=10) == "hooked"


def test_agent_key_for_process_falls_back_to_cwd_when_no_agent_found(monkeypatch):
    monkeypatch.setattr(inbox_core, "_ps_line", lambda pid: None)
    assert agent_key_for_process("/Users/ultra/work/landing", start_pid=10) == "cwd-ccfe64bae2f277d7"


# --- the consuming reader ---


def test_consume_pending_returns_entries_and_archives_at_most_once(cfg):
    d = inbox_dir("landing")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] first") + "\n" + _entry(2, "[TG from Alex tg#2] second") + "\n")
    entries = consume_pending("landing", session_id="sess-1")
    assert [e["wrapped"] for e in entries] == ["[TG from Alex tg#1] first", "[TG from Alex tg#2] second"]
    assert format_block_reason(entries) == "[TG from Alex tg#1] first\n\n[TG from Alex tg#2] second"
    assert not (d / "pending.jsonl").exists()
    assert not list(d.glob("claim-*"))
    assert not list(d.glob("*.tmp"))
    batches = list(d.glob("delivered-*.jsonl"))
    assert len(batches) == 1  # ONE complete batch file per consumption
    assert batches[0].stat().st_mode & 0o777 == 0o600
    assert d.stat().st_mode & 0o777 == 0o700
    delivered = [json.loads(line) for line in batches[0].read_text().splitlines()]
    assert [x["id"] for x in delivered] == [1, 2]
    assert all(x["session_id"] == "sess-1" and x["delivered_ts"] for x in delivered)
    # second call: nothing pending → nothing delivered again, no second batch
    assert consume_pending("landing") == []
    assert len(list(d.glob("delivered-*.jsonl"))) == 1


def test_consume_pending_missing_or_empty_never_blocks(cfg):
    assert consume_pending("nobody") == []
    d = inbox_dir("empty")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("")
    assert consume_pending("empty") == []
    assert not (d / "pending.jsonl").exists()


def test_consume_pending_malformed_lines_are_archived_flagged_not_delivered(cfg, capsys):
    d = inbox_dir("mixed")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("not json\n" + json.dumps({"nope": 1}) + "\n" + _entry(7, "ok") + "\n")
    entries = consume_pending("mixed")
    assert [e["wrapped"] for e in entries] == ["ok"]
    (batch,) = d.glob("delivered-*.jsonl")
    delivered = [json.loads(line) for line in batch.read_text().splitlines()]
    assert [x.get("malformed", False) for x in delivered] == [True, True, False]
    assert "2 malformed" in capsys.readouterr().err


def test_consume_pending_archive_failure_delivers_nothing_and_keeps_claim(cfg, monkeypatch, capsys):
    d = inbox_dir("stuck")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "x") + "\n")
    monkeypatch.setattr(inbox_core, "_write_private", lambda path, text: (_ for _ in ()).throw(OSError("disk full")))
    assert consume_pending("stuck") == []
    assert list(d.glob("claim-*"))  # records preserved for a human, not lost
    assert not list(d.glob("delivered-*"))
    assert "could not publish" in capsys.readouterr().err


def test_consume_pending_never_overwrites_a_stale_claim(cfg):
    # A claim left by an earlier archive failure must survive a later consume even if the
    # pid repeats: claim names are unique (pid + nanoseconds + random), so no rename can
    # land on it.
    d = inbox_dir("reuse")
    d.mkdir(parents=True)
    stale = d / f"claim-{os.getpid()}-1-deadbeef.jsonl"
    stale.write_text(_entry(9, "old, never archived") + "\n")
    (d / "pending.jsonl").write_text(_entry(1, "new") + "\n")
    assert [e["id"] for e in consume_pending("reuse")] == [1]
    assert stale.exists() and "old, never archived" in stale.read_text()


# --- through the real bridges, as a subprocess ---


def _run_bridge(module: str, event: dict, *, cfg: Path, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env.update({"HOME": str(home), "TG_CTL_CONFIG_DIR": str(cfg), "PYTHONPATH": str(_LIB)})
    # Pin the "agent process" to pid 1 (launchd/init — never an agent binary) so the key
    # falls back to the cwd hash. Without this the ancestry walk would climb out of pytest
    # into the developer's OWN interactive `claude --name …` and key on that name.
    env["CLAUDE_PID"] = "1"
    return subprocess.run(
        [sys.executable, "-m", module, "Stop"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_cc_bridge_stop_delivers_pending_inbox_as_block_reason(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"  # the subprocess has no agent ancestor → cwd key
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(42, "[TG from Alex tg#42] deploy now") + "\n")
    res = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "session_id": "s1", "cwd": cwd}, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"decision": "block", "reason": "[TG from Alex tg#42] deploy now"}
    assert "delivering 1 queued" in res.stderr
    assert len(list(d.glob("delivered-*.jsonl"))) == 1
    # idempotent: the next Stop has nothing pending → no block
    res2 = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "session_id": "s1", "cwd": cwd}, cfg=cfg, home=home)
    assert res2.stdout == ""


def test_cc_bridge_stop_empty_and_malformed_inbox_never_block(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    res = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "cwd": cwd}, cfg=cfg, home=home)
    assert res.returncode == 0 and res.stdout == ""
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("{{{ garbage\n")
    res = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "cwd": cwd}, cfg=cfg, home=home)
    assert res.returncode == 0 and res.stdout == ""
    assert "malformed" in res.stderr


def test_codex_bridge_stop_delivers_pending_inbox(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(5, "[TG from Alex tg#5] hi codex") + "\n")
    res = _run_bridge("codex_hook_bridge", {"hook_event_name": "Stop", "cwd": cwd}, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"decision": "block", "reason": "[TG from Alex tg#5] hi codex"}


def test_codex_bridge_ignores_inherited_claude_pid_and_keys_on_cwd(tmp_path, monkeypatch):
    # A Codex started from a Claude-owned shell inherits CLAUDE_PID; its inbox key must
    # still be the cwd hash, never the Claude session's --name.
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(5, "for codex") + "\n")
    named = cfg / "inbox" / "rig-fable"
    named.mkdir(parents=True)
    (named / "pending.jsonl").write_text(_entry(6, "for the claude session") + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env.update({"HOME": str(home), "TG_CTL_CONFIG_DIR": str(cfg), "PYTHONPATH": str(_LIB), "CLAUDE_PID": str(os.getppid())})
    res = subprocess.run(
        [sys.executable, "-m", "codex_hook_bridge", "Stop"],
        input=json.dumps({"hook_event_name": "Stop", "cwd": cwd}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert json.loads(res.stdout) == {"decision": "block", "reason": "for codex"}
    assert (named / "pending.jsonl").exists()  # untouched


def test_cc_bridge_stop_hook_block_and_inbox_ride_in_one_block(tmp_path):
    # The v1 stop descriptors ALWAYS run; a blocking one is not starved by the inbox — its
    # reason follows the queued messages in the same block.
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    gate = tmp_path / "gate.py"
    gate.write_text("#!/usr/bin/env python3\nimport sys, json; print(json.dumps({'message': 'stay: gate says no'})); sys.exit(10)\n")
    gate.chmod(0o755)
    (hooks / "gate.stop.json").write_text(json.dumps({
        "id": "gate", "point": "stop", "cmd": str(gate), "priority": 10, "timeout_ms": 3000, "on_error": "closed",
    }))
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] hi") + "\n")
    res = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "cwd": cwd}, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["decision"] == "block"
    assert out["reason"].startswith("[TG from Alex tg#1] hi\n\n")
    assert "gate says no" in out["reason"]
    # empty inbox afterwards: the gate alone still blocks
    res2 = _run_bridge("cc_hook_bridge", {"hook_event_name": "Stop", "cwd": cwd}, cfg=cfg, home=home)
    assert json.loads(res2.stdout)["reason"].strip() == "stay: gate says no"
