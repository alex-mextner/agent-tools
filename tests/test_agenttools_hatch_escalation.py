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


# --- Inline `RIG_HATCH_REQUEST_<HOOK_ID>=value` on the Bash command string ------------------
#
# A pre-bash hook runs in its OWN process BEFORE the shell evaluates a `VAR=x cmd` prefix, so the
# documented inline hatch form (`RIG_HATCH_REQUEST_X="why" <gated-command>`) never reaches the
# hook's os.environ. request_hatch_approval(command=...) must instead parse the assignment out of
# the command string the event carries. These tests pin that behavior (env is forced empty so the
# ONLY possible source of the justification is the inline command parse).

_DESTRUCTIVE_RESET = "git reset " + "--hard"


def test_inline_command_assignment_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'printf "%s" "$2" > "{question_file}"\n'
        'printf "approved by Alex\\n"\n'
        "exit 0\n",
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f'{env_var}="need to discard a disposable experiment" {_DESTRUCTIVE_RESET}'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.approved is True
    assert result.env_present is True
    assert result.should_stop is True
    question = question_file.read_text()
    assert "need to discard a disposable experiment" in question
    assert "block-reset-hard" in question


def test_inline_quoted_value_with_spaces_preserved(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'printf "%s" "$2" > "{question_file}"\nexit 0\n',
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    command = f'{env_var}="ship gate down, manual verify done" gh pr merge 123 --admin'

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "ship gate down, manual verify done" in question_file.read_text()


def test_process_env_takes_precedence_over_inline(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'printf "%s" "$2" > "{question_file}"\nexit 0\n',
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f'{env_var}="INLINE justification" {_DESTRUCTIVE_RESET}'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={env_var: "EXPORTED justification"},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    question = question_file.read_text()
    # The chosen justification is the EXPORTED one, not the inline one. Assert on the specific
    # `Justification:` line (the raw command is echoed verbatim elsewhere in the question and
    # legitimately contains the inline text, so a bare `not in` would be a false negative).
    assert "Justification: EXPORTED justification" in question
    assert "Justification: INLINE justification" not in question


def test_inline_bare_flag_is_rejected_without_tg_ctl_contact(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f"{env_var}=1 {_DESTRUCTIVE_RESET}"

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is True
    assert result.approved is False
    assert result.should_stop is True
    assert "written justification" in result.reason
    assert not marker.exists()


def test_inline_only_matches_the_hooks_own_var(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    # A DIFFERENT hook's var (and an arbitrary env) must never be consumed by this hook.
    command = f'RIG_HATCH_REQUEST_SOME_OTHER_HOOK="why" FOO=bar {_DESTRUCTIVE_RESET}'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.env_present is False
    assert result.should_stop is False
    assert not marker.exists()


def test_inline_assignment_after_executable_is_not_consumed(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    # The assignment appears as an ARGUMENT after the executable, not a leading env prefix — a
    # real shell would pass it to `git` as an argument, not set it in the environment.
    command = f'git commit -m "{env_var}=sneaky"'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.should_stop is False
    assert not marker.exists()


def test_no_command_and_empty_env_is_not_requested(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": _DESTRUCTIVE_RESET},
        cwd=str(repo),
        env={},
        command=None,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.env_present is False
    assert not marker.exists()


def test_inline_malformed_quoting_is_not_requested(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f'{env_var}="unterminated {_DESTRUCTIVE_RESET}'  # unmatched quote

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert not marker.exists()


# --- Segment-aware inline parsing (`&&` / `||` / `;` / `|` / newline) ------------------------
#
# The gated command is often not the first simple command on the line. A pre-bash hook must
# still find the leading `RIG_HATCH_REQUEST_<HOOK_ID>=…` assignment on a later segment, exactly
# as the shell would apply the `VAR=x cmd` prefix to that segment's command. The hook gates and
# (on approval) allows the WHOLE command as a unit, and the full command is shown to the human
# approver, so a leading assignment on ANY segment requests approval of the whole command.


def _tg_ctl_recording(tmp_path, question_file):
    return _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'printf "%s" "$2" > "{question_file}"\nexit 0\n',
    )


def test_inline_multi_segment_after_and_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'cd {repo} && {env_var}="no reviewer available, verified by hand" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert result.should_stop is True
    assert "no reviewer available, verified by hand" in question_file.read_text()


def test_inline_newline_separated_segment_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'cd {repo}\n{env_var}="hand-verified" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "hand-verified" in question_file.read_text()


def test_inline_semicolon_gated_segment_carries_assignment(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    # Assignment sits on the SECOND (gated) segment, after a harmless first one.
    command = f'echo starting ; {env_var}="hand-verified" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "hand-verified" in question_file.read_text()


def test_inline_whole_command_approval_from_earlier_segment(tmp_path):
    # Documented, intentional: the hook gates/allows the WHOLE command and the human approver
    # sees the full command in the tg-ctl question, so a leading assignment on an earlier segment
    # is a request to approve the entire command (never a silent bypass — approval is human).
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'{env_var}="hand-verified" echo ok ; git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.should_stop is True
    question = question_file.read_text()
    assert "Justification: hand-verified" in question
    # The full command (including the later gated segment) is shown to the approver.
    assert "git commit -m x" in question


def test_inline_quoted_separator_is_not_a_false_positive(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", f"touch {marker}\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    # A `;` inside a quoted argument must not be mistaken for a real leading assignment on a new
    # segment — there is no hatch var here at all.
    command = 'git commit -m "step one; step two"'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert not marker.exists()


# --- Quote-aware tokenization: line continuations, single `&`, quoted separators -------------
#
# The documented README examples use a `VAR="…" \`<newline>`<command>` line-continuation shape,
# and real gated commands can use a single `&` (background) separator. Quoting must be respected
# so a `;`/`&`/`|` inside the justification value is NOT treated as a command boundary.


def test_inline_line_continuation_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    # The exact README shape: `VAR="…" \` at end of line, command on the next line.
    command = f'{env_var}="ship gate down, manual verify done" \\\n  gh pr merge 123 --admin'

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "ship gate down, manual verify done" in question_file.read_text()


def test_inline_single_ampersand_separator_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("subagent-no-bg-longproc")
    command = f'sleep 1 & {env_var}="self-managed watchdog" review diff -C /repo'

    result = request_hatch_approval(
        "subagent-no-bg-longproc",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "self-managed watchdog" in question_file.read_text()


def test_inline_quoted_separator_in_value_preserved(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    # A `&`/`;` inside the quoted justification must survive as part of the value, not split it.
    command = f'{env_var}="ci & security both green; verified" gh pr merge 7 --admin'

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "Justification: ci & security both green; verified" in question_file.read_text()
