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
import shlex
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
    "flock /tmp/l -c 'git commit --no-verify'",  # flock -c runs a shell STRING (not re-parsed) — codex
])
def test_known_limitation_nested_shell_string_not_gated(command, monkeypatch):
    """KNOWN LIMITATION (matches the require-review-before-commit sibling): a commit run via a
    nested shell-string interpreter (`bash -c '…'`, `sh -c '…'`) or `xargs` is NOT re-parsed, so it
    is not gated. This is the deliberate precision trade — these are documented in the README, and
    the gate is process discipline (on_error=open), not a security boundary. This test PINS the
    current behavior so a change to it is a conscious decision, not a silent regression."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


@pytest.mark.parametrize("command", [
    "unshare git commit --no-verify",
    "nsenter -t 1 -m -- git commit --no-verify",
    "chroot /path git commit --no-verify",
    "systemd-run git commit --no-verify",
])
def test_known_limitation_unlisted_privilege_wrappers_not_gated(command, monkeypatch):
    """KNOWN LIMITATION (codex): `_WRAPPERS` is a best-effort, not exhaustive, set. Several util-linux
    privilege/namespace wrappers (`unshare`, `nsenter`, `chroot`, `systemd-run`) are NOT in it, so a
    commit wrapped in one is not peeled and not gated — the same precision trade as the `bash -c`
    nested-shell-string limitation. (`sudo` and `runuser` — the two common privilege wrappers an agent
    actually reaches for — ARE gated; see test_runuser_direct_exec_form_gated_positional_form_not.) The gate is
    PROCESS DISCIPLINE, not a security boundary: an agent that wants to bypass it can already use
    `bash -c '…'` (also not gated), so chasing every namespace wrapper is whack-a-mole with no security
    gain — and each addition carries real risk (`setpriv` was added mid-review with an incomplete flag
    set and itself re-opened the #69 bypass). Gating these correctly needs a deliberate, separately-
    reviewed pass (each has intricate operand semantics: `nsenter -t PID -m --`, `systemd-run`'s large
    flag set, `chroot DIR`). PINNED so a silent change is caught; to gate one, add it to `_WRAPPERS` +
    its value-flags + operand-drop COMPLETELY, with block AND allow tests — never half-add it."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


def test_known_limitation_env_split_string_var_expansion_not_replicated(monkeypatch):
    """KNOWN LIMITATION (codex): the `env -S` re-tokenization uses shlex, which catches the common
    bypass but does NOT replicate env's own split-string features — `${VAR}` substitution and `#`
    comments. shlex tokenizes a `${X}` executable literally (as the token `${X}`, never `git`), so a
    split-string that hides the executable behind a variable is not gated. This is the same precision
    trade as the `bash -c '…'` nested-string limitation; the gate is process discipline, not a
    security boundary. PINNED so a change to this behavior is conscious, not a silent regression."""
    out, code = _run("env -S '${X} commit --no-verify'", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "env -S 'git commit --no-verify'",                  # -S operand IS a command, re-inspected
    "env -S'git commit --no-verify'",                   # glued short -S
    "env --split-string='git commit --no-verify'",      # =-glued long form
    "env -u FOO -S 'git commit --no-verify'",           # -S after another env option
    "env -S 'sudo -u git git commit --no-verify'",      # a wrapper nested inside the -S command
    "env -S 'HUSKY=0 git commit -m x'",                 # a hook-disable env nested inside -S
    # the TAIL after the -S string is appended as the command's args, so it must NOT leak back to the
    # outer git check — `env -S 'git commit' --no-verify` really runs `git commit --no-verify` (codex)
    "env -S 'git commit' --no-verify",                  # the bypass flag is the trailing tail
    "env -S 'git' commit --no-verify",                  # subcommand + flag both in the tail
    "env --split-string='git commit' --no-verify",
    "env -u FOO -S 'git' commit --no-verify",           # value-flag, then -S, then a tail bypass
    "env -S 'sudo -u git git commit' --no-verify",      # nested wrapper + a trailing bypass flag
    # `-S` COMBINED in a short cluster (`-iS` = `-i -S`, GNU env allows it)
    "env -iS 'git commit --no-verify'",
    "env -vS 'git commit --no-verify'",
    "env -iS 'git commit' --no-verify",                 # combined short + trailing tail bypass
    # a combined value-cluster's operand must be skipped so a LATER -S is reached (codex)
    "env -iu FOO -S 'git commit --no-verify'",          # -iu FOO (combined -i -u), then -S
    "env -iP /tmp -S 'git commit --no-verify'",         # -iP /tmp (macOS), then -S
    "env --split-string 'git commit --no-verify'",      # separate long form (no `=`)
    "sudo env -S 'git commit --no-verify'",             # env -S nested INSIDE another wrapper (codex)
    "timeout 60 env -S 'git commit --no-verify'",
    # `-uS` is `-u` taking `S` as its value (NOT split-string), so the real `git commit --no-verify`
    # after it is reached and blocked — pins the S-as-value-vs-S-as-split-string boundary (codex)
    "env -uS git commit --no-verify",
    # a REAL bypass nested env-S-in-env-S WITHIN the depth cap: recursion must find it, not just the
    # cap raise (codex). depth 2 is well under _MAX_SPLIT_STRING_DEPTH.
    "env -S \"env -S 'git commit --no-verify'\"",
])
def test_blocked_env_split_string_is_reinspected(command, monkeypatch):
    """`env -S '<command>'` re-splits and runs `<command>` (plus any trailing args); its operand and
    tail must be RE-INSPECTED as a command, not swallowed as an opaque value (codex review). A bypass
    hidden in the split-string, in the appended tail, behind a combined short flag, or nested in
    another wrapper is still caught."""
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command


def test_blocked_env_split_string_pathological_nesting_fails_closed(monkeypatch):
    """A pathologically deep `env -S 'env -S …'` nesting exceeds the recursion cap and FAILS CLOSED
    (block), not open — deep obfuscation is exactly what the gate must not wave through (codex).

    The bottom command is BENIGN (`true`), so the block can ONLY come from the depth-cap `raise`, not
    from a `--no-verify` found in the body — this genuinely exercises the fail-closed branch (codex:
    the old single-quote string paired its quotes and never reached real depth). `shlex.quote` builds
    the true nesting; depth 9 > _MAX_SPLIT_STRING_DEPTH (8)."""
    deep = "true"
    for _ in range(bnv._MAX_SPLIT_STRING_DEPTH + 1):
        deep = "env -S " + shlex.quote(deep)
    # the find_bypass call must RAISE (depth cap), and main() turns that into a fail-closed block
    with pytest.raises(ValueError):
        bnv.find_bypass(deep)
    out, code = _run(deep, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_allow_env_split_string_deep_but_within_cap(monkeypatch):
    """A genuinely-nested but WITHIN-cap `env -S` chain over a benign command stays ALLOW — proving
    the block above is the depth cap, not the nesting itself."""
    deep = "true"
    for _ in range(bnv._MAX_SPLIT_STRING_DEPTH - 1):  # below the cap
        deep = "env -S " + shlex.quote(deep)
    out, code = _run(deep, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_env_split_string_exactly_at_depth_cap_still_inspected(monkeypatch):
    """The env-S depth BOUNDARY mirror (codex), matching the wrapper-nesting at-limit pair: a chain of
    EXACTLY `_MAX_SPLIT_STRING_DEPTH` levels is still inspected (the leaf is reached at depth==cap,
    only depth>cap raises). A bypass at that leaf BLOCKS via the real flag; a benign leaf ALLOWS —
    pinning the `>` vs `>=` boundary so an off-by-one is caught."""
    bypass = "git commit --no-verify"
    benign = "true"
    for _ in range(bnv._MAX_SPLIT_STRING_DEPTH):  # exactly at the cap
        bypass = "env -S " + shlex.quote(bypass)
        benign = "env -S " + shlex.quote(benign)
    out_b, code_b = _run(bypass, monkeypatch)
    assert code_b == bnv.BLOCK_EXIT_CODE and _decision(out_b) == "block"
    out_g, code_g = _run(benign, monkeypatch)
    assert code_g == 0 and _decision(out_g) == "allow"


def test_value_flag_wrappers_are_all_recognized_wrappers():
    """INVARIANT (codex): every wrapper carrying value-flags must be in `_WRAPPERS`, else
    `_strip_wrappers` never peels it and a wrapped `git commit --no-verify` slips. The module-load
    assert guards it; this test pins the invariant explicitly so a regression is caught in CI."""
    assert set(bnv._WRAPPER_VALUE_FLAGS) <= bnv._WRAPPERS


def test_operand_drop_wrappers_are_all_recognized_wrappers():
    """INVARIANT (codex): every operand-drop wrapper must be in `_WRAPPERS` too — a wrapper added to
    operand-drop but forgotten in `_WRAPPERS` would be dead config. Symmetric with the value-flag
    invariant; the module-load raise guards it, this pins it in CI."""
    assert bnv._OPERAND_DROP_WRAPPERS <= bnv._WRAPPERS


def test_mandatory_operand_wrappers_are_operand_drop_wrappers():
    """INVARIANT (codex): a mandatory-operand wrapper (its leading positional is always present and
    dropped unconditionally, even when literally `git`) must first be an operand-drop wrapper."""
    assert bnv._MANDATORY_OPERAND_WRAPPERS <= bnv._OPERAND_DROP_WRAPPERS


def test_blocked_pathological_wrapper_nesting_fails_closed(monkeypatch):
    """A `sudo … sudo git commit --no-verify` chain past `_MAX_WRAPPER_NESTING` fails CLOSED (block),
    symmetric with the `env -S` depth cap — the loop no longer exits with a wrapper still at the head
    and falls through to allow (codex)."""
    deep = "sudo " * (bnv._MAX_WRAPPER_NESTING + 1) + "git commit --no-verify"
    out, code = _run(deep, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_blocked_wrapper_nesting_exactly_at_limit_still_reaches_git(monkeypatch):
    """The BOUNDARY mirror (codex): a chain of EXACTLY `_MAX_WRAPPER_NESTING` wrappers still peels
    through to the real `git commit` and blocks via the real `--no-verify` flag — NOT via the cap.
    Paired with the cap+1 fail-closed test, this pins the off-by-one (a `>` vs `>=` slip would make
    the at-limit case raise prematurely or the over-limit case slip)."""
    at_limit = "sudo " * bnv._MAX_WRAPPER_NESTING + "git commit --no-verify"
    out, code = _run(at_limit, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"
    # and a CLEAN commit at exactly the limit must ALLOW (proving the block was the flag, not the cap)
    clean = "sudo " * bnv._MAX_WRAPPER_NESTING + "git commit -m wip"
    out2, code2 = _run(clean, monkeypatch)
    assert code2 == 0 and _decision(out2) == "allow"


def test_blocked_env_split_string_unbalanced_quote_fails_closed(monkeypatch):
    """An `env -S` whose string has an unbalanced quote cannot be tokenized → fail CLOSED (block),
    not a silent allow (codex)."""
    out, code = _run("env -S 'echo \"unbalanced'", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "env -S 'ls -la'",          # a clean split-string command must ALLOW
    "env -S 'git status'",      # a non-gated git subcommand inside -S
    "env -S 'git commit' -m wip",  # a CLEAN commit with the tail as the message, no skip flag
    "env -S 'git status' --porcelain",  # clean status with a benign tail
    "env -uS git status",       # `-u` consumes `S` as ITS value (-u S), NOT a split-string
    "exec -a x ls",             # exec -a over a non-git command
    "stdbuf -o 0 ls",           # stdbuf -o MODE over a non-git command
])
def test_allow_clean_split_string_and_exec_stdbuf(command, monkeypatch):
    """The re-inspection / new value-flags must not over-block a CLEAN command behind `env -S`,
    `exec -a`, or `stdbuf -o` — including a clean commit whose tail is a benign message, and a `-uS`
    that is `-u` with value `S` (not a split-string)."""
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
    # ── the #69 sudo-VALUE bypass: a value-flag whose operand is literally `git` ──────────────
    # `sudo -u git git commit --no-verify` has TWO `git` tokens — the FIRST is the -u user value,
    # the SECOND is the real executable. The value-flag's operand must be consumed UNCONDITIONALLY
    # so the real `git commit` is reached; the earlier `_is_git_executable` guard misread the user
    # value as the wrapped git and let the bypass slip (decision: allow). It must BLOCK.
    "sudo -u git git commit --no-verify",    # -u value is the user "git", real flag still caught
    "sudo -g git git commit --no-verify",    # -g group value is "git"
    "sudo -u git git commit -n",             # the commit -n short form behind the same bypass
    "sudo -u git git push --no-verify",      # push variant
    "sudo --user=git git commit --no-verify",  # glued `--user=git` form (not a separate operand)
    "env X=1 sudo -u git git commit --no-verify",  # env then sudo-value chain, real flag caught
    "timeout -s git git commit --no-verify",   # contrived: -s signal value "git", flag still caught
    # ── per-wrapper value-flags: a letter that is a VALUE-flag for one wrapper is BOOLEAN for sudo ──
    # `sudo -s`/`-k`/`-i` are boolean (run-shell / reset-timestamp / login-shell) — they do NOT take
    # an operand, so `sudo -s git commit --no-verify` STILL runs the real `git commit`. Treating the
    # value set as flat (not per-wrapper) ate `git` as `-s`'s value and OPENED this bypass (codex).
    "sudo -s git commit --no-verify",        # sudo -s = run shell (boolean), real flag still caught
    "sudo -k git commit --no-verify",        # sudo -k = reset timestamp (boolean)
    "sudo -i git commit --no-verify",         # sudo -i = login shell (boolean)
    "sudo -n git commit --no-verify",         # sudo -n = non-interactive (boolean), not git's -n
    "doas -s git commit --no-verify",         # doas -s = shell (boolean)
    "timeout -k 5 git commit --no-verify",    # timeout -k DOES take a value (5s), flag still caught
    "nice -n 10 git commit --no-verify",      # nice -n consumes its adjustment, real flag caught
    "ionice -c 3 git commit --no-verify",     # ionice -c consumes its class, real flag caught
    # ── completeness of the per-wrapper separate-operand sets (codex audit on this PR) ──────────
    # Each of these flags takes a SEPARATE operand; if it were missing from the wrapper's value set
    # the operand would be read as the executable and the real `git commit` behind it would be missed
    # (the same shift as the #69 bug). These pin the audited completeness.
    "sudo -D /tmp git commit --no-verify",    # sudo --chdir <dir>
    "sudo -T 30 git commit --no-verify",      # sudo --command-timeout <t>
    "sudo -U bob git commit --no-verify",     # sudo --other-user <user>
    "sudo --chdir=/tmp git commit --no-verify",  # glued long form was already safe; pin it
    "doas -a krb5 git commit --no-verify",    # doas -a <auth-style>
    "doas -C /etc/doas.conf git commit --no-verify",  # doas -C <config>
    "env -u FOO git commit --no-verify",      # env --unset <name>
    "env -C /tmp git commit --no-verify",     # env --chdir <dir> (coreutils 9+)
    "env -u FOO HUSKY=0 git commit -m x",     # env value-flag + a VAR=val that disables hooks
    "time -o out git commit --no-verify",     # GNU time --output <file>
    "time -f fmt git commit --no-verify",     # GNU time --format <fmt>
    "ionice -P 1234 git commit --no-verify",  # ionice --pgid (a harmless over-block: -P/-p act on an
    "ionice -u 1000 git commit --no-verify",  # existing process, but blocking is the safe side)
    "exec -a x git commit --no-verify",       # bash `exec -a NAME` overrides argv[0], takes operand
    "exec -a git git commit --no-verify",     # the -a value literally `git` is still the operand
    # CPU-affinity / scheduling / privilege / lock wrappers (same #69 class) — operand-drop wrappers
    "taskset -c 0 git commit --no-verify",    # taskset --cpu-list <list>
    "taskset 0x1 git commit --no-verify",     # taskset bare MASK is a positional operand
    "chrt -f 50 git commit --no-verify",      # chrt: -f policy (boolean) + 50 priority operand
    "chrt 50 git commit --no-verify",
    "chrt -d -T 10000 -P 30000 -D 300000 git commit --no-verify",  # DEADLINE sched-params take values
    "chrt --sched-runtime 10000 --sched-deadline 300000 git commit --no-verify",
    "setpriv --reuid 0 git commit --no-verify",  # setpriv value-options, no positional operand
    "setpriv --ruid 0 git commit --no-verify",   # SINGULAR id forms must be value-flags too (codex)
    "setpriv --euid 0 git commit --no-verify",
    "setpriv --rgid 0 git commit --no-verify",
    "setpriv --egid 0 git commit --no-verify",
    "setpriv --ptracer any git commit --no-verify",  # --ptracer <pid|any|none>: UNPRIVILEGED, the
    "setpriv --ptracer any sudo -u git git commit --no-verify",  # most dangerous omission (codex)
    "flock /tmp/l git commit --no-verify",    # flock FILE operand
    "flock -w 5 /tmp/l git commit --no-verify",  # flock --timeout value + FILE operand
    "flock git git commit --no-verify",       # flock LOCKFILE literally named `git` (an arbitrary
    "flock -w 5 git git commit --no-verify",  # path) must be dropped UNCONDITIONALLY — the guard that
                                              # protects timeout/taskset/chrt opened a flock bypass (codex)
    "taskset -c 0 sudo -u git git commit --no-verify",  # chained: op-drop must not eat the next sudo
    "taskset 0x1 sudo git commit --no-verify",
    "timeout 60 sudo git commit --no-verify", # op-drop wrapper followed by a wrapper, not an operand
    "sudo -E git commit --no-verify",         # -E is BOOLEAN → git reached, real flag blocks (the
                                              # block direction for a boolean flag, paired with its
                                              # allow in test_allow_sudo_value_named_git_non_skip)
    "stdbuf -o 0 git commit --no-verify",     # stdbuf -o MODE may be SEPARATE, not only glued -oL
    "stdbuf -i 0 git commit --no-verify",
    "stdbuf -e 0 git commit --no-verify",
    "env -P /foo git commit --no-verify",     # macOS env -P altpath (a LIVE local bypass — codex)
    "env -P/foo git commit --no-verify",      # glued form
    "env -iP /foo git commit --no-verify",    # COMBINED short cluster ending in the value-letter -P
    "env --argv0 x git commit --no-verify",   # GNU env --argv0 NAME overrides argv[0]
    "env --argv0=x git commit --no-verify",
    "env -a x git commit --no-verify",        # GNU env SHORT -a (= --argv0) must also be a value-flag
    "env -aname git commit --no-verify",      # glued -aname
    "env -a name -S 'git commit --no-verify'",  # -a value then a real -S split-string bypass
    "env -a git git commit --no-verify",      # the -a (argv0) value is literally `git`, like sudo -u git
    "doas -u git git commit --no-verify",     # doas named-`git` operand (#69 for a 2nd priv wrapper)
    "nice -10 git commit --no-verify",        # nice OBSOLETE glued priority `-NN` (after nice left the
                                              # operand-drop set, this rides the plain flag-skip)
    "sudo -c daemon git commit --no-verify",  # BSD sudo -c/--login-class <class>
    "sudo --login-class daemon git commit --no-verify",
    "sudo -a krb5 git commit --no-verify",    # BSD-auth sudo -a/--auth-type <type>
    "sudo --auth-type krb5 git commit --no-verify",
    "sudo --user git git commit --no-verify", # SEPARATE long form (not only glued --user=git)
    "time git commit --no-verify",            # bare `time` wrapper (no -o/-f), still peeled
    # back the sudo "audited completeness" claim with direct cases, not just reasoning (codex)
    "sudo -p 'pw with spaces' git commit --no-verify",  # sudo --prompt <str>
    "sudo -C 5 git commit --no-verify",       # sudo --close-from <fd>
    "sudo -t classroom git commit --no-verify",  # sudo --type <selinux-type>
    "sudo -r role git commit --no-verify",    # sudo --role <selinux-role>
    "sudo -R /chroot git commit --no-verify",  # sudo --chroot <dir>
])
def test_blocked_through_wrappers(command, monkeypatch):
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command


@pytest.mark.parametrize("command", [
    # a `VAR=val` between two wrappers must NOT stop the peel — the SECOND wrapper has to be peeled so
    # the real `git commit --no-verify` is reached (codex: `sudo FOO=bar timeout 5 git …` slipped).
    "sudo FOO=bar timeout 5 git commit --no-verify",
    "sudo FOO=bar env -S 'git commit --no-verify'",
    "sudo FOO=bar nice -n 5 git commit --no-verify",
    "env FOO=bar timeout 5 git commit --no-verify",
    "sudo FOO=bar taskset -c 0 git commit --no-verify",
])
def test_blocked_var_assignment_between_wrappers(command, monkeypatch):
    """A leading `VAR=val` after one wrapper, followed by ANOTHER wrapper, must still peel through to
    the real git command — the wrapper loop now collects the assignment and continues (codex)."""
    out, code = _run(command, monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block", command


@pytest.mark.parametrize("command", [
    "sudo FOO=bar timeout 5 git status",     # same chain over a non-gated subcommand must ALLOW
    "sudo FOO=bar env -S 'git status'",
    "env FOO=bar git commit -m wip",         # a clean commit behind env VAR=val
    "sudo FOO=bar git commit -m wip",
])
def test_allow_var_assignment_between_wrappers_clean(command, monkeypatch):
    """The VAR=val-between-wrappers peel must not over-block a CLEAN command behind it."""
    out, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow", command


@pytest.mark.parametrize("command", [
    # `sudo -u git <X>` where X is NOT a gated git commit/push must stay ALLOWED — the fix consumes
    # the `-u git` value but the real command behind it is clean. These prove the bypass fix did not
    # create a false positive when a value-flag's operand is literally `git`.
    "sudo -u git ls",                        # run `ls` as user git — no commit at all
    "sudo -u git git status",                # a NON-gated git subcommand behind the same value
    "sudo -u git git log --oneline",         # another non-gated subcommand
    "sudo -g git git status",                # group value variant
    "sudo --user=git git status",            # glued `--user=git`, non-gated subcommand
    "sudo -u git git commit -m wip",         # a CLEAN commit (no skip flag) as user git
    "sudo -u git git push origin main",      # a clean push as user git
    'sudo -u git echo "-u git mentions --no-verify"',  # a `-u git` value in a non-skip command
    "env X=1 sudo -u git git status",        # env + sudo-value chain, clean subcommand
    "sudo -s ls",                            # sudo -s (boolean) runs a shell with `ls` — no commit
    "sudo -k git status",                    # sudo -k (boolean) then a non-gated subcommand
    "timeout -s TERM git status",            # timeout -s consumes TERM, non-gated subcommand
    "nice -n 10 git status",                 # nice -n consumes 10, non-gated subcommand
    "sudo -D /tmp git status",               # sudo --chdir consumes /tmp, non-gated subcommand
    "sudo -T 30 git status",                 # sudo --command-timeout consumes 30
    "env -u FOO git status",                 # env --unset consumes FOO, non-gated subcommand
    "time -o out ls",                        # GNU time --output consumes a file, no git at all
    "ionice -P 1234 git status",             # ionice --pgid consumes 1234
    "env VAR=1 git commit -m wip",           # a plain env VAR=val (no value-flag) clean commit
    "env -P /usr/bin git status",            # macOS env -P consumes the path, non-gated subcommand
    "env -iP /usr/bin git status",           # combined -iP consumes the path
    "env --argv0 x git status",              # env --argv0 consumes its name
    "env -a x git status",                   # env short -a consumes its name, non-gated subcommand
    "env -a git git status",                 # env -a value literally `git`, then a non-gated subcommand
    "doas -u git git status",                # doas -u value `git` then a non-gated subcommand
    "sudo -c daemon git status",             # sudo --login-class consumes daemon
    "sudo -a krb5 git status",               # sudo --auth-type consumes krb5, non-gated subcommand
    "sudo -E git commit -m wip",             # sudo -E is BOOLEAN — a clean commit must allow
    "sudo -k git commit -m wip",             # sudo -k is BOOLEAN
    "env -iP /tmp -S 'git status'",          # combined cluster + -S over a clean command
    "env -iu FOO -S 'ls -la'",               # combined cluster + -S over a non-git command
    # `sudo -h <host>` (separate form) is the ONE consciously-excluded sudo separate-operand flag:
    # `-h` has an OPTIONAL arg, so the gate treats it as boolean and `myhost` ends up read as the
    # command → ALLOW. This is non-exploitable (sudoers has no remote-command plugin, so a real
    # `sudo -h myhost git commit` does not run git). PINNED so the conscious exclusion is documented.
    "sudo -h myhost git commit --no-verify",
    # the GLUED form `sudo -hHOST` (codex): `-hmyhost` ends in `t` = sudo's `--type` value-letter, so
    # the last-char heuristic in _cluster_takes_next_value eats the next token (`git`) → ALLOW. Same
    # documented, non-exploitable outcome; PINNED so a heuristic change is a conscious decision.
    "sudo -hmyhost git commit --no-verify",
    # the new CPU/sched/priv/lock wrappers over a CLEAN command must ALLOW (operand-drop reaches git)
    "taskset -c 0 git status",
    "taskset 0x1 git status",
    "chrt -f 50 git status",
    "chrt -d -T 10000 -P 30000 -D 300000 git status",  # deadline sched-params consumed, non-gated cmd
    "setpriv --reuid 0 git status",
    "setpriv --ptracer any git status",       # --ptracer value-flag consumes 'any', non-gated cmd
    "setpriv --ruid 0 git status",
    "flock /tmp/l git status",
    "flock /tmp/l ls",                        # flock over a non-git command
    "flock git git status",                   # lockfile named `git`, then a non-gated subcommand
    "flock git ls",                           # lockfile named `git`, then a non-git command
    "taskset -c 0 git commit -m wip",         # a CLEAN commit behind taskset
    "timeout 60 sudo git status",             # op-drop wrapper then sudo over a non-gated subcommand
])
def test_allow_sudo_value_named_git_non_skip(command, monkeypatch):
    """REGRESSION (#69 sudo-value bypass fix): a value-flag operand that is literally `git`
    (`sudo -u git …`) is consumed as the operand, NOT misread as the wrapped executable. When the
    command behind it is a non-gated git subcommand or a clean commit/push, it must ALLOW — the fix
    that makes `sudo -u git git commit --no-verify` BLOCK must not over-block these."""
    out, code = _run(command, monkeypatch)
    assert code == 0, (command, out)
    assert _decision(out) == "allow", command


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


def test_crux_sudo_value_bypass_fixed(monkeypatch):
    """SUDO-VALUE-BYPASS-FIXED (#69 follow-up): `sudo -u git git commit --no-verify` has two `git`
    tokens — the first is the `-u` user value, the second is the real executable. The old guard
    misread the user value as the wrapped git and ALLOWED the bypass; the fix consumes the value
    unconditionally and BLOCKS, while a clean `sudo -u git git status` still ALLOWS."""
    assert bnv.find_bypass("sudo -u git git commit --no-verify") is not None
    assert bnv.find_bypass("sudo -g git git commit -n") is not None
    assert bnv.find_bypass("env X=1 sudo -u git git commit --no-verify") is not None
    assert bnv.find_bypass("sudo -u git git status") is None
    assert bnv.find_bypass("sudo -u git ls") is None
    out, code = _run("sudo -u git git commit --no-verify", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_crux_per_wrapper_value_flags_no_boolean_collision(monkeypatch):
    """PER-WRAPPER-VALUE-FLAGS (codex review on this PR): a flag that takes a VALUE for one wrapper
    is BOOLEAN for another. `sudo -s`/`-k`/`-i` are boolean and `sudo -s git commit --no-verify`
    STILL runs the real `git commit` → it must BLOCK; making the value set flat (not per-wrapper)
    would eat `git` as `-s`'s value and OPEN that bypass. `timeout -s TERM`/`nice -n 10` keep their
    real value-flags, and a boolean-flag wrapper over a NON-git command (`sudo -s ls`) ALLOWS."""
    assert bnv.find_bypass("sudo -s git commit --no-verify") is not None
    assert bnv.find_bypass("sudo -k git commit --no-verify") is not None
    assert bnv.find_bypass("doas -s git commit --no-verify") is not None
    assert bnv.find_bypass("timeout -s TERM git commit --no-verify") is not None
    assert bnv.find_bypass("nice -n 10 git commit --no-verify") is not None
    assert bnv.find_bypass("sudo -s ls") is None
    assert bnv.find_bypass("timeout -s TERM ls") is None
    # runuser: `-s` is its SHELL-path VALUE flag (boolean for sudo, signal-value for timeout — three
    # different meanings for the same letter), and its booleans (`-l`/`-f`/`-m`/`-p`/`-P`) must NOT
    # eat the wrapped git. Pin both so a future flag edit to the new wrapper can't regress silently.
    assert bnv.find_bypass("runuser -s /bin/sh -u git -- git commit --no-verify") is not None
    assert bnv.find_bypass("runuser -l -u git -- git commit --no-verify") is not None
    assert bnv.find_bypass("runuser -s /bin/sh -u alice -- ls") is None


def test_runuser_direct_exec_form_gated_positional_form_not(monkeypatch):
    """RUNUSER, the privilege sibling of sudo. Only its `-u user [--] command` form DIRECTLY execs the
    command (the same #69 class as `sudo -u git git commit`): `-u` consumes the user value (even when
    literally `git`), an optional `--` is skipped, the real `git commit` is reached and BLOCKED. The
    su-compatible `[-] user [args]` form passes its trailing args to the user's login SHELL rather than
    exec'ing them, so `runuser alice git commit --no-verify` does NOT run `git commit` — it is NOT a
    bypass and is intentionally left ungated (gating it would add only over-block; its exact exec
    semantics are version/shell-dependent and unverifiable on this host). `-c`/`--command` carry a
    command STRING, out of scope like `bash -c`."""
    # `-u user [--] command` direct-exec form → BLOCK (commit + push, -n + --no-verify, with/without --).
    assert bnv.find_bypass("runuser -u git -- git commit --no-verify") is not None
    assert bnv.find_bypass("runuser -u alice git commit --no-verify") is not None
    assert bnv.find_bypass("runuser -u git -- git push --no-verify") is not None
    assert bnv.find_bypass("runuser -g git -u alice git commit -n") is not None
    # long `--user` form (separate value path) and the glued `--user=git` form. The `=` form blocks
    # because the whole `--user=git` token is dropped as a flag (its value stays glued, never leaks as
    # a bare `git`); pin both so a future `--flag=value` matching change can't silently over/under-block.
    assert bnv.find_bypass("runuser --user git -- git commit --no-verify") is not None
    assert bnv.find_bypass("runuser --user=git -- git commit --no-verify") is not None
    # Each remaining value-flag's consumption is load-bearing: if one ever drops out of the set its
    # value `git` would be read as the wrapped executable and the real `git commit` behind it would be
    # MISSED (a bypass, not just over-block). Pin -G/--supp-group, -w/--whitelist-environment and
    # --session-command (a value form `-c` lacks) so a future flag-list edit can't silently re-open it.
    assert bnv.find_bypass("runuser -G git -u alice git commit -n") is not None
    assert bnv.find_bypass("runuser --supp-group git -u alice git commit -n") is not None
    assert bnv.find_bypass("runuser -w VAR=1 -u git -- git commit --no-verify") is not None
    assert bnv.find_bypass("runuser --session-command x -u git -- git commit -n") is not None
    # benign `-u` form → allow (no false positive)
    assert bnv.find_bypass("runuser -u git -- git status") is None
    assert bnv.find_bypass("runuser -u alice git commit -m ok") is None
    # `-c`/`--command` STRING is out of scope (the command is inside the quoted value, like `bash -c`)
    assert bnv.find_bypass("runuser -c 'git commit --no-verify' git") is None
    # su-compatible POSITIONAL form: args go to the user's shell, not a direct exec → NOT gated.
    # Pinned so a future operand-drop change can't silently start gating (over-blocking) this form.
    assert bnv.find_bypass("runuser alice git commit --no-verify") is None
    assert bnv.find_bypass("runuser alice git status") is None
    assert bnv.find_bypass("runuser deploy ./release.sh") is None
    assert bnv.find_bypass("runuser - alice git commit --no-verify") is None   # bare login-dash form
    assert bnv.find_bypass("runuser git git commit --no-verify") is None       # user literally `git`
    # NARROW residual over-block: a user literally named `git` with a command whose name (`commit`) is
    # a git subcommand is indistinguishable from `git commit` at argv[0]. `git` is a common username,
    # but a command literally named `commit`/`push` is near-nonexistent, so this is accepted + pinned.
    assert bnv.find_bypass("runuser git commit --no-verify") is not None
    # full hook path: the direct-exec vector BLOCKS, the positional form ALLOWS.
    out, code = _run("runuser -u git -- git commit --no-verify", monkeypatch)
    assert code == bnv.BLOCK_EXIT_CODE and _decision(out) == "block"
    out, code = _run("runuser alice git commit --no-verify", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
