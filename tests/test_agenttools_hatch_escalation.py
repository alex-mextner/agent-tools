"""Tests for the shared agent-hook Telegram hatch escalation helper."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_hatch_escalation
from agenttools_hatch_escalation import hatch_env_var, request_hatch_approval


def _write_tg_ctl(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _repo_with_tg_ctl(tmp_path: Path, tg_ctl: Path | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = ["agent_hooks:"]
    if tg_ctl is not None:
        lines.append(f'  tg_ctl_path: "{tg_ctl}"')
    (repo / "rig.yaml").write_text("\n".join(lines) + "\n")
    return repo


def test_env_unset_is_not_requested_and_does_not_contact_tg_ctl(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.approved is False
    assert result.env_present is False
    assert result.should_stop is False
    assert "not set" in result.reason
    assert not marker.exists()


def test_whitespace_env_is_not_requested_and_does_not_contact_tg_ctl(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(repo),
        env={env_var: " \t\n "},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.approved is False
    assert result.env_present is True
    assert result.should_stop is True
    assert "blank" in result.reason
    assert not marker.exists()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_bare_flag_env_is_rejected_without_tg_ctl_contact(tmp_path, value):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: value},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is True
    assert result.approved is False
    assert result.env_present is True
    assert result.should_stop is True
    assert "written justification" in result.reason
    assert not marker.exists()


def test_real_justification_exit0_allows_and_includes_context(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'printf "%s" "$2" > "{question_file}"\n'
        'printf "approved by Alex\\n"\n'
        "exit 0\n",
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"repo": str(repo), "target": "feat/x", "command": "git checkout feat/x"},
        cwd=str(repo),
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.approved is True
    assert "approved by Alex" in result.reason
    question = question_file.read_text()
    assert "pin-primary-worktree" in question
    assert "Need to inspect the primary checkout before repairing it." in question
    assert "git checkout feat/x" in question
    assert result.tg_ctl_path == str(tg_ctl.resolve())


def test_real_justification_exit1_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 1\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "denied" in result.reason


def test_real_justification_unavailable_tg_ctl_denies(tmp_path):
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tmp_path / "missing" / "tg-ctl"],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "not available" in result.reason


def test_real_justification_unexpected_exit_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 2\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "denied" in result.reason


def test_real_justification_timeout_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "sleep 5\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=0.2,
        process_margin_s=0.2,
    )

    assert result.requested is True
    assert result.approved is False
    assert "timed out" in result.reason


def test_path_shadowing_does_not_choose_fake_tg_ctl(tmp_path, monkeypatch):
    real_marker = tmp_path / "real-called"
    shadow_marker = tmp_path / "shadow-called"
    real = _write_tg_ctl(
        tmp_path / "real" / "tg-ctl",
        f"touch {real_marker}\n"
        'printf "approved via real path\\n"\n'
        "exit 0\n",
    )
    trusted_link = tmp_path / "trusted" / "tg-ctl"
    trusted_link.parent.mkdir()
    trusted_link.symlink_to(real)
    shadow = _write_tg_ctl(
        tmp_path / "shadow" / "tg-ctl",
        f"touch {shadow_marker}\n"
        'printf "shadow approved\\n"\n'
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", f"{shadow.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[trusted_link],
        timeout_s=1,
    )

    assert result.approved is True
    assert real_marker.exists()
    assert not shadow_marker.exists()
    assert shadow.exists()
    assert result.tg_ctl_path == str(real.resolve())


def test_rig_yaml_tg_ctl_path_override_is_used(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(
        tmp_path / "reviewed-config" / "tg-ctl",
        f"touch {marker}\n"
        'printf "approved via rig config\\n"\n'
        "exit 0\n",
    )
    repo = _repo_with_tg_ctl(tmp_path, tg_ctl)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(repo),
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[],
        timeout_s=1,
    )

    assert result.approved is True
    assert marker.exists()
    assert result.tg_ctl_path == str(tg_ctl.resolve())


def test_default_tg_ctl_candidates_are_hardcoded_absolute_paths():
    assert Path("/Users/ultra/.files/bin/tg-ctl") in agenttools_hatch_escalation._TRUSTED_TG_CTL_PATHS
    assert all(path.is_absolute() for path in agenttools_hatch_escalation._TRUSTED_TG_CTL_PATHS)


_AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1] / "agent-hooks"


@pytest.mark.parametrize(
    "descriptor",
    [
        _AGENT_HOOKS_DIR / "block-reset-hard" / "block-reset-hard.pre-bash.json",
        _AGENT_HOOKS_DIR / "pin-primary-worktree" / "pin-primary-worktree.pre-bash.json",
    ],
)
def test_descriptor_timeout_strictly_exceeds_helper_worst_case(descriptor):
    """The descriptor's `timeout_ms` must strictly exceed the helper's worst-case wall time.

    The helper waits up to `MAX_TG_CTL_TIMEOUT_S` for tg-ctl plus `DEFAULT_PROCESS_MARGIN_S`
    before it kills the subprocess and returns its own deny. If the descriptor budget merely
    EQUALLED that worst case, a hung/unanswered tg-ctl could race the bridge's descriptor-timeout
    kill (which resolves via `on_error`, fail-OPEN for pin-primary-worktree) into a silent allow
    instead of the helper's intended deny (Codex PR #200 P2, tg#6554). A strict margin guarantees
    the helper resolves first. Regression guard: keep timeout_ms > (cap + margin) * 1000.
    """
    worst_case_ms = (
        agenttools_hatch_escalation.MAX_TG_CTL_TIMEOUT_S
        + agenttools_hatch_escalation.DEFAULT_PROCESS_MARGIN_S
    ) * 1000.0
    timeout_ms = json.loads(descriptor.read_text())["timeout_ms"]
    assert timeout_ms > worst_case_ms, (
        f"{descriptor.name}: timeout_ms={timeout_ms} must strictly exceed the helper's "
        f"worst case {worst_case_ms:.0f}ms so the helper's deny wins the race"
    )
