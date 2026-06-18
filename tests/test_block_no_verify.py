"""Tests for the block-no-verify agent-hook (pre-bash, fail-CLOSED commit/push gate).

The hook denies a command that bypasses the pre-commit gate. The crux: the verdict is made from
the PARSED command, never a raw substring — so the gate proves BOTH directions:

  ALLOW (was FALSELY blocked by the old raw regex):
    - a commit MESSAGE that mentions "--no-verify"/"-n"/"core.hooksPath"/"commit" (`-m`, `-F`,
      `-F -` heredoc) → the words are message text, not flags;
    - a SIBLING command in a chain carrying `-n` (`grep -n x && git commit -m ok`);
    - committing a FILE whose path/content mentions no-verify.

  BLOCK (real bypass, incl. WRAPPED / fused-separator / positioned):
    - `git commit --no-verify`, `git commit -n`, `git push --no-verify`;
    - `git -c core.hooksPath=/dev/null commit` (a real `-c` config that disables hooks);
    - `timeout 60 git commit --no-verify` (wrapper-peeled);
    - `x;git commit --no-verify` (fused separator);
    - inline `HUSKY=0`/`SKIP=…`/`LEFTHOOK=0` env tricks.

  FAIL-CLOSED: an unparseable command (unbalanced quotes) BLOCKS — the existing safe default.

This is a pure string-parse hook (no git subprocess), so the tests feed a command via the stdin
JSON event and assert the decision — no temp repo needed.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_block_no_verify.py -q
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
    / "block-no-verify"
    / "block_no_verify.py"
)
_spec = importlib.util.spec_from_file_location("block_no_verify", _HOOK)
assert _spec and _spec.loader
bnv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bnv)


def _run(command: str, monkeypatch) -> tuple[str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = bnv.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── ALLOWED: the words appear in a MESSAGE / VALUE / SIBLING, not as a real flag ──────────────

@pytest.mark.parametrize("command", [
    # the trigger words inside a commit MESSAGE (the real false-block this PR fixes)
    'git commit -m "mention --no-verify in the message"',
    'git commit -m "remove the -n flag from docs"',
    'git commit -m "document core.hooksPath behavior"',
    'git commit -m "fix: the commit gate"',
    # env-disable TOKENS inside a message — the old raw regex substring-matched these (headline FP)
    'git commit -m "set SKIP=lint in CI"',
    'git commit -m "explain HUSKY=0 in the docs"',
    'git commit -m "LEFTHOOK=0 disables hooks"',
    "git commit -am 'add --no-verify to the changelog'",
    'git commit --message="explain -n and --no-verify"',
    # the words inside a -F message FILE path / a value flag, never read as a flag
    "git commit -F .git/COMMIT_NO_VERIFY_NOTES",
    "git commit --file=docs/no-verify.md",
    "git commit --author='No Verify <n@v>' -m ok",
    "git commit --author '--no-verify' -m ok",   # --no-verify is the AUTHOR value, not a flag
    "git commit -F --no-verify",                  # --no-verify is the -F file PATH, not a flag
    "git commit -Skeyid -m ok",                   # -S glued value, no real -n
    "git commit --gpg-sign=keyid -m ok",          # --gpg-sign glued value
    "git commit -Sname@example -m ok",            # -S keyid containing 'n' is data, not a -n flag
    "git commit -Sn -m ok",                       # -S optional glued value 'n', not --no-verify
    "git commit -mn",                             # -m takes "n" as the glued message, no -n flag
    "git commit -mno-verify",                     # -m glued message "no-verify", not a flag
    "git commit -amn wip",                        # -a, -m takes "n" glued → no real -n flag
    "git push -n && git push -n",                 # both dry-run pushes, never no-verify
    # a SIBLING command in a chain carries `-n`; the commit itself is clean
    "grep -n no-verify README.md && git commit -m ok",
    "echo -n done && git commit -m fine",
    "git diff -n && git commit -m ok",
    # `git push -n` is --dry-run, NOT --no-verify (codex finding #1) → must be ALLOWED
    "git push -n",
    "git push -n origin main",
    "git push --dry-run origin main",
    "git diff -n && git push -n",                 # push dry-run alongside a sibling -n
    # committing a file whose PATH mentions no-verify
    "git add src/no-verify.ts && git commit -m feat",
    # a plain commit/push with nothing suspicious
    "git commit -m wip",
    "git push origin main",
    "git -c user.name=bot commit -m ok",  # a harmless `-c`, not core.hooksPath
    "git commit --no-ver -m x",           # AMBIGUOUS prefix git rejects → not a bypass
    "git commit --no-edit -m x",          # a different --no-* flag, not no-verify
    "git commit -m normal > log.txt",     # a REAL redirect is still stripped, commit is clean
    "git commit -m x 2> err.log",         # a genuine fd redirect (2>) is stripped, commit is clean
    "git commit -m x 2>&1",
    "git -c core.hooksPath=/dev/null status",  # hookspath on a NON-gated subcommand → allow
])
def test_allowed_commands(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == 0, (command, out)
    assert _decision(out) == "allow", command


def test_allow_heredoc_message_mentioning_no_verify(monkeypatch):
    """A `-F -` heredoc whose BODY mentions the trigger words: the body is data (stripped before
    tokenization), so none is a commit/push flag → ALLOW. The old raw regex blocked this."""
    command = (
        "git commit -F - <<'EOF'\n"
        "feat: parse the command\n\n"
        "This commit is about --no-verify and -n and core.hooksPath.\n"
        "EOF"
    )
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("body", [
    "git commit --no-verify",            # a body LINE that itself looks like a bypass command
    "HUSKY=0 make",                      # a body line that looks like a hook-disable env command
    "git -c core.hooksPath=/dev/null commit",
])
def test_allow_heredoc_body_line_that_looks_like_a_command(body, monkeypatch):
    """The heredoc BODY is data fed to stdin, never shell commands — so a body line that LOOKS like
    a real bypass (`git commit --no-verify`) must NOT trip the gate (codex review finding #1). The
    opener `git commit -F -` is the only real command and it is clean."""
    command = f"git commit -F - <<'EOF'\n{body}\nEOF"
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


def test_blocked_quoted_heredoc_marker_does_not_eat_real_command(monkeypatch):
    """A `<<WORD` INSIDE a quoted string is NOT a heredoc opener — so it must not swallow a FOLLOWING
    real `git commit --no-verify` as if it were heredoc body (codex review finding #2). The real
    bypass on the next line is still caught."""
    command = 'echo "see <<NOTE in docs"\ngit commit --no-verify'
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_block_real_heredoc_with_dash_indent(monkeypatch):
    """A `<<-END` indented heredoc strips its body; the opener `cat <<-END` is not a commit, but a
    REAL `git commit --no-verify` AFTER the terminator is still gated."""
    command = "cat <<-END\nnot a real command\nEND\ngit commit --no-verify"
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_allow_push_dash_o_option_value_is_no_verify(monkeypatch):
    """`git push -o --no-verify` / `--push-option=--no-verify`: `--no-verify` is the push-option
    VALUE (a server string), not a flag → ALLOW (codex review finding #4)."""
    for command in ("git push -o --no-verify", "git push --push-option=--no-verify"):
        out, code = _run(command, monkeypatch)
        assert code == 0 and _decision(out) == "allow", command


def test_block_push_dash_o_then_real_no_verify(monkeypatch):
    """A push-option value is consumed, but a SUBSEQUENT real `--no-verify` is still caught."""
    out, code = _run("git push -o ci.skip --no-verify", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_allow_no_verify_word_in_trailing_comment(monkeypatch):
    out, code = _run("git commit -m ok  # remember --no-verify is blocked", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "git commit -m '#42fix'",            # a quoted message that STARTS with '#'
    "git commit -m '#'",
    "git commit -m 'fix #42'",           # '#' mid-message
])
def test_allow_quoted_hash_message(command, monkeypatch):
    """A quoted message starting with (or containing) '#' is NOT a shell comment — it must be kept,
    not truncated. The comment scan runs on the RAW line respecting quotes (codex HIGH finding)."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


@pytest.mark.parametrize("command", [
    "git commit -m '#wip' --no-verify",  # the '#wip' message must NOT swallow the real flag after it
    "git commit -m '#' --no-verify",
])
def test_block_real_no_verify_after_quoted_hash_message(command, monkeypatch):
    """A quoted `#`-leading message used to be misread as a comment, dropping it AND the trailing
    real `--no-verify` → a BYPASS. The quote-aware comment scan keeps the flag visible (codex)."""
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block", command


@pytest.mark.parametrize("command", [
    "(git commit --no-verify)",                       # subshell
    "{ git commit --no-verify; }",                    # brace group
    "if true; then git commit --no-verify; fi",       # if/then keyword
    "! git commit --no-verify",                       # negation
    "while true; do git commit --no-verify; done",    # while/do
    "until git commit --no-verify; do true; done",    # until
    "for x in a b; do git commit -n; done",           # for/do, commit -n inside
])
def test_block_no_verify_behind_grouping_or_keyword(command, monkeypatch):
    """A `git commit --no-verify` wrapped in a subshell `( … )`, brace group `{ …; }`, or after a
    control keyword (`then`) is still gated — the leading grouping/keyword noise is stripped to
    recover the real command (codex review finding #2)."""
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_allow_non_git_command_with_no_verify_word(monkeypatch):
    out, code = _run("echo 'use --no-verify to skip' > notes.txt", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "bash -lc 'git commit --no-verify'",   # nested `bash -c` string is not re-parsed
    "sh -c 'git commit --no-verify'",
    "xargs git commit --no-verify",        # xargs is not a known wrapper
])
def test_known_limitation_nested_shell_string_not_gated(command, monkeypatch):
    """KNOWN LIMITATION (matches the require-review-before-commit sibling): a commit run via a
    nested shell-string interpreter (`bash -c '…'`, `sh -c '…'`) or `xargs` is NOT re-parsed, so it
    is not gated. This is the deliberate precision trade — these are documented in the README, and
    the gate is process discipline (on_error=open), not a security boundary. This test PINS the
    current behavior so a change to it is a conscious decision, not a silent regression."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


# ── BLOCKED: a REAL bypass flag/config on a real commit/push segment ──────────────────────────

@pytest.mark.parametrize("command", [
    "git commit --no-verify",
    "git commit -m wip --no-verify",
    "git commit --no-verify -m wip",     # flag before the message
    "git commit -n",
    "git commit -n -m wip",
    "git push --no-verify",
    "git push --no-verify origin main",
    "/usr/bin/git commit --no-verify",   # git via an absolute path
    "git -c user.name=x commit --no-verify",  # harmless -c does not hide the real flag
    "git commit -am wip -n",             # bare -n after a message cluster
    "git commit -nm wip",                # cluster: -n flag BEFORE the -m message-taker
    "git commit -vn",                    # cluster: -v then -n
    "git commit -nam wip",               # -n flag, then -a, then -m takes "wip"
    "git commit -S --no-verify",         # -S (gpg-sign) value is OPTIONAL/glued, not the next token
    "git commit --gpg-sign --no-verify",  # so --no-verify is a real flag, not -S's value (codex)
    "git commit -S -n -m x",
    "git commit --no-veri -m x",         # unambiguous abbreviation git resolves to --no-verify
    "git commit --no-verif -m x",
    "git push --no-verif",
    "git commit -m '<' --no-verify",     # a quoted '<' message must not eat the real flag (redirect)
    "git commit -m '>>' --no-verify",
    "git commit -m 2 > log --no-verify",  # the digit '2' is -m's message, not an fd → flag survives
])
def test_blocked_real_no_verify(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command
    assert "no-verify" in json.loads(out)["message"].lower()


@pytest.mark.parametrize("command", [
    "git -c core.hooksPath=/dev/null commit -m wip",
    "git -c core.hooksPath= commit -m wip",
    "git -c core.hookspath=/dev/null commit -m wip",   # git config keys are case-insensitive
    "git -ccore.hooksPath=/dev/null commit -m wip",    # glued -c form
    "git -c core.hooksPath=/dev/null push",
    "git -c core.hooksPath commit -m x",   # bare key (no `=`) → boolean true → hooks skipped (codex)
])
def test_blocked_hookspath_config(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command
    assert "hookspath" in json.loads(out)["message"].lower()


@pytest.mark.parametrize("command", [
    "timeout 60 git commit --no-verify",
    "timeout 1m git commit -n",
    "nice -n 10 git commit --no-verify",   # nice's own -n must NOT be confused with git's -n
    "env FOO=bar git commit --no-verify",
    "stdbuf -oL git push --no-verify",
    "nohup git commit --no-verify",
    "sudo git commit --no-verify",          # sudo prefix peeled (codex review finding #2)
    "sudo -u alice git commit --no-verify",  # sudo's -u value is skipped, then the real flag caught
    "doas git commit --no-verify",
    "time git commit --no-verify",
    "command git commit --no-verify",
    "setsid git commit --no-verify",
    "ionice git commit --no-verify",
    "unbuffer git commit --no-verify",
    "sudo FOO=bar git commit --no-verify",  # sudo's own VAR=val operand is peeled, real flag caught
    "/usr/bin/sudo git commit --no-verify",  # a PATH-qualified wrapper is matched by basename (codex)
    "/usr/bin/timeout 60 git commit --no-verify",
    "exec git commit --no-verify",           # `exec` is a transparent prefix (codex)
])
def test_blocked_through_wrappers(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command


@pytest.mark.parametrize("command", [
    "echo hi;git commit --no-verify",          # fused `;` separator
    "x=1&&git commit --no-verify",             # fused `&&` separator
    "true | git commit --no-verify",           # piped
    "git add -A\ngit commit --no-verify",      # newline-separated multi-line
    "git add -A && git commit -n",             # real chain, flag on the commit
])
def test_blocked_fused_and_chained(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command


@pytest.mark.parametrize("command", [
    "HUSKY=0 git commit -m wip",
    "LEFTHOOK=0 git commit -m wip",
    "SKIP=flake8 git commit -m wip",
    "GIT_HOOKS_SKIP=1 git commit -m wip",
    "PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m wip",
    "HUSKY=0 make",                  # a NON-git command with a hook-disabling env still blocks
    "env HUSKY=0 make",              # via the `env` wrapper too (codex review finding #3 symmetry)
    "SKIP=lint pre-commit run",
    "export HUSKY=0; git commit -m x",   # `export` builtin sets the env for the shell (codex)
    "export HUSKY=0 && git commit -m x",
    "export SKIP=lint",
    "declare LEFTHOOK=0",
    "sudo HUSKY=0 git commit -m x",      # sudo's own VAR=val env operand reaches the check (codex)
])
def test_blocked_hook_disable_env(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command
    assert "hooks disabled" in json.loads(out)["message"].lower()


def test_allow_husky_nonzero_value(monkeypatch):
    """`HUSKY=1` does NOT disable hooks (only `HUSKY=0` does), so it must not block."""
    out, code = _run("HUSKY=1 git commit -m wip", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "export HUSKY=1; git commit -m wip",     # HUSKY=1 does not disable hooks
    "export PATH=/usr/bin && git commit -m x",  # an unrelated export is fine
    "export FOO=bar && git commit -m x",
])
def test_allow_harmless_export(command, monkeypatch):
    """An `export` of an unrelated var (or `HUSKY=1`) must not block — only a hook-DISABLING
    assignment does."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


# ── FAIL-CLOSED: unparseable / malformed input still DENIES ───────────────────────────────────

def test_unbalanced_quotes_fail_closed(monkeypatch):
    """An unparseable command (unbalanced quote) cannot be inspected → BLOCK (fail-closed)."""
    out, code = _run("git commit -m 'unterminated --no-verify", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_malformed_event_fail_closed(monkeypatch):
    """A non-JSON stdin event blocks (fail-closed) — a bypass through a broken gate is the very
    failure this hook exists to stop."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json{"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = bnv.main()
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out.getvalue()) == "block"


# ── unit-level: the parser helpers behave on the load-bearing cases ───────────────────────────

def test_find_bypass_returns_none_for_clean_commit():
    assert bnv.find_bypass("git commit -m 'fix --no-verify docs'") is None


def test_find_bypass_flags_real_no_verify():
    assert bnv.find_bypass("git commit --no-verify") is not None


def test_find_bypass_raises_on_unparseable():
    with pytest.raises(ValueError):
        bnv.find_bypass("git commit -m 'unterminated")


# ── the three CRUX acceptance directions, named for the proof ─────────────────────────────────

def test_crux_false_positive_fixed_message_mentions_trigger(monkeypatch):
    """FP-FIXED: the old raw regex BLOCKED a commit whose MESSAGE mentions the trigger words; the
    parsed gate ALLOWS it (the words are message text, not a flag)."""
    out, code = _run('git commit -m "this is about --no-verify and -n"', monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert bnv.find_bypass('git commit -m "about --no-verify and -n"') is None


def test_crux_false_negative_fixed_wrapped_and_fused(monkeypatch):
    """FN-FIXED: a genuine `--no-verify` hidden behind a wrapper / a fused separator was MISSED by
    the flat regex; the parsed gate still catches it."""
    for command in ("timeout 60 git commit --no-verify", "x=1&&git commit --no-verify"):
        out, code = _run(command, monkeypatch)
        assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_crux_push_dash_n_is_dry_run_allowed(monkeypatch):
    """PUSH-`-n`-ALLOWED: for `git push`, bare `-n` is `--dry-run`, NOT `--no-verify` → ALLOW.
    A `git push --no-verify` (the LONG flag) is still BLOCKED."""
    out, code = _run("git push -n origin main", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert bnv.find_bypass("git push -n") is None
    assert bnv.find_bypass("git push --no-verify") is not None
    # and a real commit -n is still a bypass (commit -n IS --no-verify)
    assert bnv.find_bypass("git commit -n") is not None


def test_blocked_env_wrapper_husky(monkeypatch):
    """`env HUSKY=0 git commit` — the `env` wrapper's own `VAR=val` reaches the env check and the
    hook-disabling assignment BLOCKS (codex finding #1)."""
    out, code = _run("env HUSKY=0 git commit -m wip", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
