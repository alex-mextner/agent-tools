"""Tests for agent-hooks/pkill-guard/pkill_guard.py.

Covers:
  - True positives: `pkill -f <shared-name>`, `killall <shared-name>`,
    `kill $(pgrep -f <shared-name>)`, `` kill `pgrep -f <shared-name>` ``,
    `pgrep <shared-name> | xargs kill`, and the "narrow grep" pipeline shape
    (`ps aux | grep <shared-name> | ... | xargs kill`) are blocked.
  - The two real incidents this hook exists for: `pkill -f "review diff"` and a
    grep-into-kill pipeline targeting a shared name.
  - Session-scoped patterns are ALLOWED even when they also name a shared tool (a path, the
    harness's isolation-prefix token, a hex/uuid-looking run).
  - `kill <pid>` (bare, multiple PIDs, with -SIGNAL) is always allowed.
  - An unrecognized/unlisted pattern is allowed (fails open on unknowns).
  - `dev stop` and unrelated commands are allowed.
  - Wrapped forms (`sudo pkill -f node`, `timeout 5 killall node`) don't defeat detection.
  - Shell chains: the dangerous form behind `&&`/`;`/newline is still caught.
  - External Telegram hatch: unset denies (block), a real justification + tg-ctl exit 0
    allows, tg-ctl exit nonzero denies, a blank/bare-flag value denies without asking.
  - Fail-closed: unbalanced quotes with a plausible dangerous hint, and a malformed event.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_pkill_guard.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "pkill-guard"
    / "pkill_guard.py"
)
_spec = importlib.util.spec_from_file_location("pkill_guard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# A throwaway cwd — this hook has no rig.yaml walk-up (only the Telegram hatch), but keep the
# same hermetic-cwd discipline as the sibling hook tests.
_HERMETIC_CWD = tempfile.mkdtemp(prefix="pkg-hermetic-")


def _run(command: str, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`. Returns (stdout, stderr, exit)."""
    event: dict = {"args": {"command": command}, "cwd": _HERMETIC_CWD}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_HATCH_REQUEST_PKILL_GUARD", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── True positives: direct pkill/killall of a shared name — BLOCK ──────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        'pkill -f "review diff"',  # the actual 2026-06-26 incident shape
        "pkill -f node",
        "pkill -9 -f codex",
        "killall node",
        "killall -9 claude",
    ],
)
def test_block_direct_pattern_kill_of_shared_name(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_names_pid_and_dev_stop_alternatives(monkeypatch):
    out, _err, _code = _run("pkill -f node", monkeypatch)
    msg = json.loads(out)["message"]
    assert "kill <pid>" in msg
    assert "dev stop" in msg


# ── True positives: kill fed a pgrep substitution — BLOCK ───────────────────────────────────

def test_block_kill_dollar_paren_pgrep(monkeypatch):
    out, _err, code = _run('kill $(pgrep -f codex)', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_kill_backtick_pgrep(monkeypatch):
    out, _err, code = _run("kill `pgrep -f playwright`", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_kill_dollar_paren_pgrep_with_signal(monkeypatch):
    out, _err, code = _run('kill -9 $(pgrep -f "review diff")', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── True positives: pipeline forms — BLOCK ──────────────────────────────────────────────────

def test_block_pgrep_pipe_xargs_kill(monkeypatch):
    out, _err, code = _run("pgrep -f playwright | xargs kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pgrep_pipe_xargs_kill_with_flags(monkeypatch):
    out, _err, code = _run("pgrep -f node | xargs -I{} kill -9 {}", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_narrow_grep_pipeline(monkeypatch):
    """The 2026-06-27 incident shape: ps | grep <shared-name> | awk | xargs kill."""
    out, _err, code = _run(
        "ps aux | grep claude | awk '{print $2}' | xargs kill -9", monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pgrep_pipe_bare_kill(monkeypatch):
    out, _err, code = _run("pgrep -f node | kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Allowed: session-scoped patterns, even naming a shared tool ────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        'pkill -9 -f "hvsc-3-a1b2c3d4"',  # the sanctioned e2e-harness recipe
        'pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/node_modules/.bin/vitest"',
        'killall -9 "node-worker-8f3a9c21"',
        'pkill -f "codex-session-4471"',
    ],
)
def test_allow_session_scoped_pattern(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Allowed: PID-targeted kill ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "kill 12345",
        "kill -9 12345",
        "kill -s TERM 12345",
        "kill 111 222 333",
        "kill %1",
    ],
)
def test_allow_pid_targeted_kill(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Allowed: unrecognized pattern, unrelated commands, dev stop ────────────────────────────

def test_allow_unlisted_process_name(monkeypatch):
    out, _err, code = _run('pkill -f "my-custom-project-server"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_dev_stop(monkeypatch):
    out, _err, code = _run("dev stop", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_unrelated_command(monkeypatch):
    out, _err, code = _run("git status", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_mentioning_pkill_as_text(monkeypatch):
    """Text that merely mentions pkill/node — no pkill/kill invocation — is allowed."""
    out, _err, code = _run('grep -r "pkill -f node" docs/', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_ps_grep_without_kill_stage(monkeypatch):
    """A pipeline that merely LOOKS at processes (no kill/xargs-kill stage) is allowed."""
    out, _err, code = _run("ps aux | grep node", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Wrapped forms don't defeat detection ────────────────────────────────────────────────────

def test_block_sudo_wrapped(monkeypatch):
    out, _err, code = _run("sudo pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_timeout_wrapped(monkeypatch):
    out, _err, code = _run("timeout 5 killall node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_sudo_with_user_flag_wrapped(monkeypatch):
    out, _err, code = _run("sudo -u alice pkill -f codex", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Shell chains: the dangerous form behind &&/;/newline is still caught ───────────────────

def test_block_behind_and_and(monkeypatch):
    out, _err, code = _run("echo hi && pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_behind_semicolon(monkeypatch):
    out, _err, code = _run("cd /repo; pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_on_second_line(monkeypatch):
    out, _err, code = _run("cd /repo\npkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Fail-closed paths ────────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_with_dangerous_hint_blocks(monkeypatch):
    out, _err, code = _run("pkill -f 'node --unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_command_allowed(monkeypatch):
    out, _err, code = _run("echo won't fail", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_malformed_event_blocks(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", err_buf)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out_buf.getvalue()) == "block"


def test_missing_command_field_allows(monkeypatch):
    """No command at all (an event with an empty args) is trivially safe."""
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── External Telegram hatch escalation ──────────────────────────────────────────────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_hatch_unset_denies(monkeypatch):
    out, _err, code = _run("pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_hatch_blank_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("pkill -f node", monkeypatch, {"RIG_HATCH_REQUEST_PKILL_GUARD": "   "})
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert not (tmp_path / "asked").exists()


def test_hatch_bare_flag_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("pkill -f node", monkeypatch, {"RIG_HATCH_REQUEST_PKILL_GUARD": "1"})
    assert code == hook.BLOCK_EXIT_CODE
    assert not (tmp_path / "asked").exists()


def test_hatch_exit0_allows(monkeypatch, tmp_path):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\nprintf 'approved by Telegram tap\\n'\nexit 0\n",
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "pkill -f node",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PKILL_GUARD": "Cleaning up my own stray dev-server instances."},
    )
    assert code == 0
    assert _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_exit_nonzero_denies(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "killall node",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PKILL_GUARD": "Need a one-off exception."},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert "hatch escalation denied" in json.loads(out)["message"].lower()
