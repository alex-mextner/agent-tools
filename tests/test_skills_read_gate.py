"""Tests for the skills-read-gate agent-hook (pre-bash).

Covers the doctrine's four cases. Hooks 4-5 have no subagent exemption; the third case is
instead the SATISFIED-MARKER path (every mandatory skill marker fresh => allow). So:
  BLOCK   — a work action (commit/build) with a missing mandatory skill, on a repeat.
  ALLOW   — a non-work command (nothing to gate), and the first-offense WARN.
  MARKER  — all mandatory markers fresh => allow even on a work action.
  ESCAPE  — env+reason and inline sentinel allow; reasonless still blocks.

Hermetic: both the invoked-markers dir and the warn/block tier dir are redirected into
tmp_path via env (and the module constants re-pointed), so nothing touches the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_skills_read_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "skills-read-gate"
    / "skills_read_gate.py"
)
_spec = importlib.util.spec_from_file_location("skills_read_gate", _HOOK)
assert _spec and _spec.loader
srg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srg)

_MANDATORY = "delegate-work-to-subagents,visual-proof-cycle"


def _run(command, monkeypatch, *, invoked: Path, tier: Path,
         env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo", "args": {"command": command}})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
    monkeypatch.setattr(srg, "TIER_DIR", tier)
    monkeypatch.setenv("MANDATORY_SKILLS", _MANDATORY)
    for k in ("ALLOW_SKIP_SKILLS", "ALLOW_SKIP_SKILLS_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = srg.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _touch_all_skills(invoked: Path) -> None:
    invoked.mkdir(parents=True, exist_ok=True)
    for skill in _MANDATORY.split(","):
        (invoked / skill).write_text("x")


# ── BLOCK (missing skill, on repeat) ───────────────────────────────────────────────────

def test_block_commit_missing_skills_on_repeat(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # first work action → WARN (allow + message)
    out1, _e1, c1 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow"
    assert "not invoked" in json.loads(out1)["message"]
    # repeat in the same cwd → BLOCK
    out2, _e2, c2 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_block_build_missing_skills_on_repeat(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("npm run build", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("npm run build", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW (non-work command) ───────────────────────────────────────────────────────────

def test_allow_non_work_command(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git status", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_allow_merge_continue_is_not_a_commit(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git commit --continue", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


# ── SATISFIED MARKER (the honest action) ───────────────────────────────────────────────

def test_allow_when_all_mandatory_skills_invoked(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)
    # even on what would otherwise be a repeat, fresh markers satisfy the gate
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"
    out2, _e2, c2 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c2 == 0 and _decision(out2) == "allow"


# ── ESCAPE ─────────────────────────────────────────────────────────────────────────────

def test_escape_env_reason_allows(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # prime warn marker
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier,
        env={"ALLOW_SKIP_SKILLS": "1", "ALLOW_SKIP_SKILLS_REASON": "docs-only"},
    )
    assert c == 0 and _decision(out) == "allow"


def test_escape_inline_sentinel_allows(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git commit -m x  # skills-ok: trivial bump",
                      monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_reasonless_override_still_blocks_on_repeat(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      env={"ALLOW_SKIP_SKILLS": "1"})  # no reason
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B2: the GIT_COMMIT regex must NOT match `git`+`commit` in plain prose ────────────────

@pytest.mark.parametrize("command", [
    'echo "remember to git, then commit"',
    'echo "git status is fine; commit later"',
])
def test_git_commit_prose_is_not_a_work_action(command, tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # even on a "repeat" it stays ALLOW because it is not a work action at all (no gating)
    _run(command, monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_git_with_global_flags_commit_is_still_gated(tmp_path, monkeypatch):
    """`git -C path commit` (global flag before subcommand) must still be recognised."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git -C /repo commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git -C /repo commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B6: extra build runners (deno/mvn/gradle/rake/msbuild) are gated ─────────────────────

@pytest.mark.parametrize("command", [
    "deno test", "mvn verify", "gradle build", "rake test", "msbuild proj.sln /t:build",
])
def test_extra_build_runners_are_work_actions(command, tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── #4: BUILD_OR_TEST must be anchored at a command head, not match inside a string ──────

def test_commit_message_mentioning_npm_test_is_a_commit_not_a_build(tmp_path, monkeypatch):
    """`git commit -m "fix: npm test was flaky"` must be gated via the COMMIT path (it IS a
    commit), not mis-classified as a build action by the `npm test` substring in the message."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    cmd = 'git commit -m "fix: npm test was flaky"'
    # the substring `npm test` lives inside the commit message → BUILD_OR_TEST must NOT fire on it
    assert not srg.BUILD_OR_TEST.search(cmd)
    # but it is a real commit, so the gate still fires (warn → block on repeat)
    out1, _e1, c1 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    'echo "see npm test output"',
    'echo "run yarn build later"',
    'grep -r "pytest" .',
])
def test_build_or_test_substring_in_a_string_is_not_a_work_action(command, tmp_path, monkeypatch):
    """A build/test word buried in a string argument (not at a command head) is not work."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", ["npm test", "npm run build", "pytest -q"])
def test_real_build_at_command_head_still_blocks_on_repeat(command, tmp_path, monkeypatch):
    """A REAL build/test at the command head is still a work action (block-on-repeat path)."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── T1: NOT subagent-exempt — an agent_id present must STILL block (locks the doctrine) ──

def test_blocks_even_with_agent_id_present(tmp_path, monkeypatch):
    """skills-read-gate is NOT subagent-exempt: a subagent doing work must also have read its
    skills. An `agent_id` in the event must NOT exempt the work action."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": "/repo", "agent_id": "sub-x",
             "args": {"command": "git commit -m x", "agent_id": "sub-x"}}
    # prime the warn tier, then assert the repeat BLOCKS despite agent_id
    for _ in range(2):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
        monkeypatch.setattr(srg, "TIER_DIR", tier)
        monkeypatch.setenv("MANDATORY_SKILLS", _MANDATORY)
        for k in ("ALLOW_SKIP_SKILLS", "ALLOW_SKIP_SKILLS_REASON"):
            monkeypatch.delenv(k, raising=False)
        code = srg.main()
    assert code == srg.BLOCK_EXIT_CODE and _decision(out.getvalue()) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
