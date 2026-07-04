"""Tests for agent-hooks/block-reset-hard/block_reset_hard.py.

Covers:
  - True positives: `git reset --hard` (bare / with ref) and `git clean -f...` (any
    short-flag clustering with -d/-x) are blocked.
  - Safe alternatives are ALLOWED: checkout/restore, bare/--mixed/--soft reset,
    `git clean -n`/no-force.
  - The argv-parse FP fix: text that merely MENTIONS "reset --hard"/"clean -fd" (a commit
    message, a comment, a grep) is ALLOWED.
  - Wrapped forms (`timeout N git ...`, `sudo git ...`) and git global options
    (`git -C <dir> ...`) don't defeat detection.
  - Shell chains: the dangerous form behind `&&`/`;` is still caught.
  - Escape hatches: env pair (with/without reason) + inline sentinel, for BOTH forms.
  - Fail-closed: unbalanced quotes (hint-matched vs. unrelated) and a malformed event.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_block_reset_hard.py -q
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
    / "block-reset-hard"
    / "block_reset_hard.py"
)
_spec = importlib.util.spec_from_file_location("block_reset_hard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(command: str, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`.  Returns (stdout, stderr, exit)."""
    event = {"args": {"command": command}}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear escape-hatch env so ambient values don't leak into tests.
    for k in ("ALLOW_GIT_RESET_HARD", "ALLOW_GIT_RESET_HARD_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── True positives: git reset --hard — should BLOCK ────────────────────────────────────────

def test_block_bare_reset_hard(monkeypatch):
    out, _err, code = _run("git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_relative_ref(monkeypatch):
    out, _err, code = _run("git reset --hard HEAD~3", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_remote_ref(monkeypatch):
    out, _err, code = _run("git reset --hard origin/main", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── True positives: git clean -f... — should BLOCK ─────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "git clean -fd",
        "git clean -df",
        "git clean -fdx",
        "git clean -xdf",
        "git clean -f -d",
        "git clean --force --force",
        "git clean -f",
        "git clean --force",
        "git clean -nf",  # -n does not cancel a real -f present in the clustering
        "git clean -fn",
        "git clean -fe*.o",  # f before e: a real force flag, not part of -e's value
    ],
)
def test_block_clean_force_variants(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


# ── Safe alternatives — should ALLOW ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- file.txt",
        "git restore file.txt",
        "git reset",
        "git reset --mixed",
        "git reset --soft HEAD~1",
        "git clean -n",
        "git clean --dry-run",
        "git clean",
        "git clean -n -e*.conf",  # dry-run with an exclude pattern, no force
        "git clean -ef*.o",  # -e consumes "f*.o" as its pattern VALUE, not a force flag
    ],
)
def test_allow_safe_alternatives(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow", command


# ── The FP fix — text merely mentioning the phrase must be ALLOWED ─────────────────────────

def test_allow_commit_message_mentioning_reset_hard(monkeypatch):
    cmd = 'git commit -m "remember: never run git reset --hard on a shared checkout"'
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_echo_mentioning_reset_hard(monkeypatch):
    out, _err, code = _run('echo "the phrase reset --hard is dangerous"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_for_clean_fd_string(monkeypatch):
    out, _err, code = _run("grep -r 'clean -fd' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Wrapped forms — should still BLOCK ─────────────────────────────────────────────────────

def test_block_wrapped_timeout_reset_hard(monkeypatch):
    out, _err, code = _run("timeout 60 git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_wrapped_sudo_clean_fd(monkeypatch):
    out, _err, code = _run("sudo git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_git_global_dash_c_reset_hard(monkeypatch):
    """`git -C <dir> reset --hard` must not evade detection via a global option."""
    out, _err, code = _run("git -C /some/path reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_git_global_no_pager_clean_fd(monkeypatch):
    out, _err, code = _run("git --no-pager clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_path_qualified_git(monkeypatch):
    """/opt/homebrew/bin/git reset --hard must still be blocked (basename check)."""
    out, _err, code = _run("/opt/homebrew/bin/git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Additional wrapper forms (ported from block-no-verify's wrapper table) — should BLOCK ──

@pytest.mark.parametrize(
    "command",
    [
        "command git reset --hard",
        "exec git reset --hard",
        "time git reset --hard",
        "setsid git clean -fd",
        "nohup git reset --hard",
        "sudo -u git git reset --hard",  # value-flag consumes "git" as the -u operand
        "nice -n 10 git reset --hard",  # value-flag consumes "10" as the -n operand
        "env -u FOO git reset --hard",  # value-flag consumes "FOO" as the -u operand
    ],
)
def test_block_additional_wrapper_forms(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


def test_block_while_loop_control_flow(monkeypatch):
    """`while`/`do` control-flow tokens must not shield the dangerous command inside."""
    out, _err, code = _run("while git clean -fd; do echo x; done", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_if_then_control_flow_reset_hard(monkeypatch):
    """`if ... ; then` must not shield a `git reset --hard` inside."""
    out, _err, code = _run("if git reset --hard; then echo x; fi", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_if_then_control_flow_clean_force(monkeypatch):
    out, _err, code = _run("if git clean -fd; then echo x; fi", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_wrapper_chain_overflow_fails_closed(monkeypatch):
    """A wrapper chain deeper than the nesting cap must BLOCK (fail-closed), never silently
    allow just because the real command couldn't be resolved through the chain."""
    command = ("command " * (hook._MAX_WRAPPER_NESTING + 4)) + "git reset --hard"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Multi-line commands and mid-word `#` — should still BLOCK ─────────────────────────────
# A single Bash tool call spanning two lines (`cd /repo` then `git reset --hard` on the next
# line) is a common, entirely ACCIDENTAL shape — the literal incident this hook guards
# against, replayed through a two-line command. A flat single-line shlex pass misses it
# entirely (the newline is just whitespace, so line one's `cd`/`echo` becomes argv[0]).

def test_block_reset_hard_on_second_line(monkeypatch):
    out, _err, code = _run("cd /repo\ngit reset --hard origin/main", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_clean_force_on_second_line(monkeypatch):
    out, _err, code = _run("echo starting cleanup\ngit clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_mid_word_hash_does_not_truncate_parsing(monkeypatch):
    """A `#` in the MIDDLE of a word (`foo#bar`) is literal text to a real shell, not a
    comment start — it must not truncate parsing and hide a later chained command."""
    out, _err, code = _run("echo foo#bar && git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_hash_inside_commit_message(monkeypatch):
    """A `#` inside a quoted commit message (`fix #42`) is message text, not a comment —
    must not be misparsed either way, and the command overall is still safe (no reset/clean)."""
    out, _err, code = _run("git commit -m 'fix #42'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "echo '# heading' && git reset --hard",
        'echo "#hi" && git reset --hard',
    ],
)
def test_block_quoted_word_initial_hash_does_not_fake_a_comment(command, monkeypatch):
    """A QUOTED argument that starts with `#` (`'# heading'`) dequotes to the same string a
    real unquoted comment would produce. A naive `tok.startswith("#")` check on the dequoted
    token alone can't tell them apart and would wrongly treat the rest of the line — including
    a chained `&& git reset --hard` — as inert comment text, silently letting it through."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


# ── Shell chains — should still BLOCK ──────────────────────────────────────────────────────

def test_block_reset_hard_in_shell_chain_and(monkeypatch):
    out, _err, code = _run("echo done && git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_clean_force_in_shell_chain_semicolon(monkeypatch):
    out, _err, code = _run("echo done; git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_leading_env(monkeypatch):
    out, _err, code = _run("GIT_TRACE=1 git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Escape hatches — reset --hard ──────────────────────────────────────────────────────────

def test_env_override_with_reason_allows_reset_hard(monkeypatch):
    out, _err, code = _run(
        "git reset --hard",
        monkeypatch,
        {"ALLOW_GIT_RESET_HARD": "1", "ALLOW_GIT_RESET_HARD_REASON": "recovering bad worktree"},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_env_override_without_reason_still_blocks_reset_hard(monkeypatch):
    out, _err, code = _run(
        "git reset --hard",
        monkeypatch,
        {"ALLOW_GIT_RESET_HARD": "1"},  # no reason → stays blocked
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_inline_sentinel_with_reason_allows_reset_hard(monkeypatch):
    out, _err, code = _run(
        "git reset --hard  # no-reset-guard: confirmed no other session uses this checkout",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_sentinel_without_reason_blocks_reset_hard(monkeypatch):
    out, _err, code = _run("git reset --hard  # no-reset-guard:", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Escape hatches — clean -f... (same hatch name gates both forms) ───────────────────────

def test_env_override_with_reason_allows_clean_force(monkeypatch):
    out, _err, code = _run(
        "git clean -fd",
        monkeypatch,
        {"ALLOW_GIT_RESET_HARD": "1", "ALLOW_GIT_RESET_HARD_REASON": "aborted experiment cleanup"},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_sentinel_with_reason_allows_clean_force(monkeypatch):
    out, _err, code = _run(
        "git clean -fd  # no-reset-guard: aborted experiment, verified nothing else needed",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


# ── Fail-closed paths ──────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_reset_hard_hint_blocks(monkeypatch):
    """Unbalanced quote on a command that plausibly is reset --hard → fail closed (block)."""
    out, _err, code = _run("git reset --hard --author 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_clean_force_hint_blocks(monkeypatch):
    out, _err, code = _run("git clean -fd --exclude 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_allows(monkeypatch):
    """Unbalanced quote on an unrelated command → allow (not a reset/clean attempt)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unbalanced_quotes_with_escape_hatch_still_blocks(monkeypatch):
    """An unparseable command is NOT eligible for the escape hatch (mirrors block-raw-pr-merge):
    fix the quoting before the override is even consulted."""
    out, _err, code = _run(
        "git reset --hard --author 'unclosed  # no-reset-guard: deliberate",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_malformed_event_blocks(monkeypatch):
    """A JSON parse error on the event → fail closed."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_empty_command_allows(monkeypatch):
    """An empty command string has no segments → nothing to block."""
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
