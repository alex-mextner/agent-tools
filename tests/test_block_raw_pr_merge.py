"""Tests for agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py.

Covers:
  - True positives: `gh pr merge <N>` is blocked
  - The argv-parse FP fix: commands whose body/args contain "gh pr merge" as TEXT
    (e.g. `gh pr create --body "...gh pr merge..."`) are ALLOWED
  - Sanctioned paths: `gh ship`, `pr-ship.sh`, `ship.sh` are allowed
  - Shell chains: `gh pr merge` behind `&&`/`;`/`||` is caught
  - Inline env assignments before `gh` are stripped
  - The DEAD self-service escape hatch (`ALLOW_RAW_PR_MERGE` / `# no-ship-guard:` no longer
    allow) and the external Telegram hatch (`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE`)
  - Fail-closed: unbalanced quotes and malformed event both block

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_block_raw_pr_merge.py -q
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
    / "block-raw-pr-merge"
    / "block_raw_pr_merge.py"
)
_spec = importlib.util.spec_from_file_location("block_raw_pr_merge", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(command: str, monkeypatch, env: dict | None = None, cwd=None) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`.  Returns (stdout, stderr, exit)."""
    event = {"args": {"command": command}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear escape-hatch env so ambient values don't leak into tests.
    for k in ("ALLOW_RAW_PR_MERGE", "ALLOW_RAW_PR_MERGE_REASON",
              "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# ── True positives — should BLOCK ──────────────────────────────────────────────────────────

def test_block_basic_gh_pr_merge(monkeypatch):
    out, _err, code = _run("gh pr merge 123", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_squash(monkeypatch):
    out, _err, code = _run("gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_admin(monkeypatch):
    out, _err, code = _run("gh pr merge 42 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_leading_env(monkeypatch):
    """A VAR=value prefix before `gh` must not prevent detection."""
    out, _err, code = _run("GH_TOKEN=secret gh pr merge 7 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_and(monkeypatch):
    """A `gh pr merge` after `&&` in a chain must still be blocked."""
    out, _err, code = _run("echo done && gh pr merge 8 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_semicolon(monkeypatch):
    out, _err, code = _run("echo done; gh pr merge 9", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_or(monkeypatch):
    out, _err, code = _run("false || gh pr merge 10", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── The FP fix — commands with "gh pr merge" as text must be ALLOWED ───────────────────────

def test_allow_gh_pr_create_with_merge_in_body(monkeypatch):
    """THE KEY BUG FIX: gh pr create whose --body contains 'gh pr merge' must NOT be blocked."""
    cmd = 'gh pr create --title "My PR" --body "After review, run gh pr merge 5 to land it"'
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_comment_with_merge_in_body(monkeypatch):
    """gh pr comment whose body text includes 'gh pr merge' must be allowed."""
    cmd = "gh pr comment 5 --body 'use gh pr merge to land this'"
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_echo_with_merge_text(monkeypatch):
    """An echo command printing 'gh pr merge' must not trigger the block."""
    out, _err, code = _run('echo "remember to gh pr merge 123"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_for_merge_string(monkeypatch):
    """grep searching for 'gh pr merge' in a file must not be blocked."""
    out, _err, code = _run("grep -r 'gh pr merge' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Sanctioned paths must be ALLOWED ───────────────────────────────────────────────────────

def test_allow_gh_ship(monkeypatch):
    out, _err, code = _run("gh ship 123", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_view(monkeypatch):
    out, _err, code = _run("gh pr view 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_create(monkeypatch):
    out, _err, code = _run("gh pr create --title 'feat: add X'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_list(monkeypatch):
    out, _err, code = _run("gh pr list", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_checkout(monkeypatch):
    out, _err, code = _run("gh pr checkout 7", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_ship_sh(monkeypatch):
    out, _err, code = _run("./ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pr_ship_sh(monkeypatch):
    out, _err, code = _run("./pr-ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── REGRESSION: the OLD self-service escape hatch is DEAD ────────────────────────────────────

def test_env_override_no_longer_allows(monkeypatch):
    """REGRESSION: `ALLOW_RAW_PR_MERGE=1` (+ `_REASON`) used to allow a raw merge — it must NO
    LONGER; the merge still BLOCKs."""
    out, _err, code = _run(
        "gh pr merge 5 --squash",
        monkeypatch,
        {"ALLOW_RAW_PR_MERGE": "1", "ALLOW_RAW_PR_MERGE_REASON": "CI provider outage"},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_inline_sentinel_no_longer_allows(monkeypatch):
    """REGRESSION: an inline `# no-ship-guard: <reason>` sentinel no longer allows — still BLOCK."""
    out, _err, code = _run(
        "gh pr merge 5 --admin  # no-ship-guard: hotfix during provider outage",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── external Telegram hatch escalation (replaces the OLD self-service escape hatch) ──────────
_HATCH_ENV = "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"


def test_hatch_unset_blocks_with_howto(monkeypatch):
    out, _err, code = _run("gh pr merge 5", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert _HATCH_ENV in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is an invalid request: deny WITHOUT contacting Telegram (the fake would touch a
    marker — it must not exist)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("gh pr merge 5", monkeypatch, {_HATCH_ENV: "1"}, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" 'printf "approved by Telegram tap\\n"\n' "exit 0\n",
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_justification_exit1_blocks_citing_denied(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── Path-qualified and subshell forms — should BLOCK ───────────────────────────────────────

def test_block_path_qualified_gh(monkeypatch):
    """/opt/homebrew/bin/gh pr merge 5 must still be blocked (basename check)."""
    out, _err, code = _run("/opt/homebrew/bin/gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_subshell_gh_pr_merge(monkeypatch):
    """( gh pr merge 5 ) — subshell grouping must not bypass detection."""
    out, _err, code = _run("( gh pr merge 5 )", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_brace_group_gh_pr_merge(monkeypatch):
    """{ gh pr merge 5; } — brace grouping must not bypass detection."""
    out, _err, code = _run("{ gh pr merge 5; }", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Fail-closed paths ──────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_merge_blocks(monkeypatch):
    """Unbalanced quote on a command containing a merge pattern → fail closed (block)."""
    out, _err, code = _run("gh pr merge 5 --body 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_allows(monkeypatch):
    """Unbalanced quote on an unrelated command → allow (not a merge attempt)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


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


# ── `\`-newline line continuation must not hide the merge (Codex review on #231) ────────────


def test_line_continuation_does_not_hide_merge():
    """shlex (whitespace_split=False) emits a standalone `\\n` token for a `\\`-newline line
    continuation. After the leading `VAR=` assignment is stripped that token would become
    argv[0], so `_is_gh_pr_merge` saw `argv[0] == '\\n'` and reported NO merge → the raw merge was
    ALLOWED with no approval. Detection must survive the continuation."""
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="x" \\\n  gh pr merge 123 --admin'
    assert hook._command_contains_gh_pr_merge(command) is True


def test_hatch_inline_line_continuation_reaches_tg_ctl(tmp_path, monkeypatch):
    """End-to-end for the exact README inline-hatch shape (`VAR="why" \\`+newline+`gh pr merge`):
    it must be DETECTED as a raw merge AND route the inline justification to tg-ctl (env var NOT
    exported). If the `\\`-newline still hid the merge, the hook would `emit("allow")` WITHOUT
    ever calling the mocked tg-ctl, so the question file would not be written."""
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f'printf "%s" "$2" > "{question}"\nexit 0\n')
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down, manual verify done" \\\n'
        '  gh pr merge 123 --admin'
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)  # env NOT set — inline only
    assert code == 0 and _decision(out) == "allow"
    assert "ship gate down, manual verify done" in question.read_text()


# ── bare-newline command separator: a merge on the 2nd line must not evade detection ────────


def test_bare_newline_second_line_merge_is_detected(monkeypatch):
    """A BARE newline separates commands in shell, so `echo ok`+newline+`gh pr merge 1` is a real
    raw merge on the second line and must BLOCK. shlex consumes a bare newline as whitespace, so
    without normalizing it to a `;` separator the two lines collapse into one `echo`-headed
    segment and the merge evades the gate (Codex review on #231)."""
    out, _err, code = _run("echo ok\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_bare_newline_after_env_assignment_merge_is_detected(monkeypatch):
    """`cd repo`+newline+`gh pr merge` — the merge on the second line is still detected."""
    out, _err, code = _run("cd repo\ngh pr merge 7 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_newline_inside_quoted_message_is_not_a_merge(monkeypatch):
    """A newline INSIDE a quoted argument must NOT be treated as a command separator — a two-line
    commit message merely mentioning a merge is not a `gh pr merge` invocation and must ALLOW."""
    out, _err, code = _run('git commit -m "line one\ngh pr merge in prose"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_comment_on_first_line_does_not_swallow_second_line_merge(monkeypatch):
    """A `#` comment runs only to the END OF ITS LINE. `echo ok # comment`+newline+`gh pr merge`
    must still BLOCK the merge on the second line. A naive `newline → ;` normalization that ignores
    comments regresses here: shlex then treats `#` as a comment to end-of-INPUT and swallows the
    merge, ALLOWING it (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run("echo ok # comment\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_comment_containing_a_merge_on_one_line_is_not_a_merge(monkeypatch):
    """A `#` comment that merely MENTIONS a merge on a single line is not a real invocation and must
    ALLOW — the comment (incl. any `;`/`gh pr merge` text inside it) is stripped to end of line."""
    out, _err, code = _run("echo done # then gh pr merge 5 --admin", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "g\\\nh pr merge 1 --admin",  # continuation INSIDE `gh`
        "gh p\\\nr merge 1 --admin",  # continuation INSIDE `pr`
        "gh pr m\\\nerge 1 --admin",  # continuation INSIDE `merge`
    ],
)
def test_line_continuation_inside_the_command_word_is_detected(command, monkeypatch):
    """A `\\`-newline is REMOVED by the shell (the parts join with NO space), so `g\\`+newline+`h pr
    merge` executes as `gh pr merge`. Normalizing the continuation to a SPACE instead would split it
    into `g h pr merge` (argv[0] == 'g') and ALLOW the raw merge — the continuation must be removed,
    not spaced (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_quote_outside_quotes_does_not_spoof_quote_state(monkeypatch):
    r"""A backslash-escaped `\"` is a LITERAL `"`, not an opening quote. `echo \"`+newline+`gh pr
    merge 1\"` runs a real merge on the second line; a scanner that ignores the `\` would treat the
    `"` as opening a quote, swallow the newline as quoted content, and ALLOW the merge (Codex review
    on the bare-newline follow-up)."""
    out, _err, code = _run('echo \\"\ngh pr merge 1 \\"', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_quote_inside_double_quotes_does_not_close_the_string(monkeypatch):
    r"""Inside a double-quoted string a `\"` is an escaped literal `"` that does NOT close the
    string. `echo "a \" b"`+newline+`gh pr merge 1`: the string closes at the SECOND real `"`, the
    newline is then unquoted and separates the real merge on line 2 — must BLOCK."""
    out, _err, code = _run('echo "a \\" b"\ngh pr merge 1 --admin', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo foo#bar && gh pr merge 1 --admin",  # mid-word `#`, same-line `&&` merge
        "echo foo#bar\ngh pr merge 1 --admin",  # mid-word `#`, second-line merge
        "echo http://x/#frag\ngh pr merge 1 --admin",  # `#` fragment in a URL, second-line merge
    ],
)
def test_midword_hash_is_not_a_comment_and_does_not_hide_merge(command, monkeypatch):
    """A `#` that is NOT at a word boundary (`foo#bar`, a URL `#frag`) is LITERAL in a real shell,
    not a comment. shlex's default commenter would truncate the whole line at that `#` and drop a
    following `gh pr merge`, allowing it. Must BLOCK (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_space_before_hash_does_not_start_a_comment(monkeypatch):
    r"""An ESCAPED space (`foo\ `) is a literal space still INSIDE the word, so a following `#` is
    NOT a comment boundary. `echo foo\ # x ; gh pr merge 1` runs the merge in a real shell; a
    scanner keying comment-start off the last emitted char (a space) would wrongly treat `#` as a
    comment, drop the rest, and ALLOW the merge (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run("echo foo\\ # x ; gh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\ngh pr merge 1 --admin\nEOF",  # body text is not a command
        "cat <<'EOF'\ngh pr merge 1\nEOF",  # quoted delimiter
        "git commit -F - <<-EOF\n\tgh pr merge 1 in the message\n\tEOF",  # `<<-` tab-stripped
    ],
)
def test_heredoc_body_mentioning_merge_is_not_blocked(command, monkeypatch):
    """A `gh pr merge` inside a HEREDOC BODY is document text, not an executed command, and must
    ALLOW — the body's newlines must not be split into `;` segments (Codex review on the follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_real_merge_before_a_heredoc_is_still_detected(monkeypatch):
    """The heredoc skip must not blind the gate to a REAL merge on the SAME line before the body."""
    out, _err, code = _run("gh pr merge 1 --admin <<EOF\nunrelated body\nEOF", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_real_merge_after_a_heredoc_is_still_detected(monkeypatch):
    """A real merge on a line AFTER the heredoc terminator must still be detected."""
    out, _err, code = _run("cat <<EOF\nbody\nEOF\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_punctuated_heredoc_delimiter_does_not_hide_a_later_merge(monkeypatch):
    """A punctuated delimiter (`EOF-MSG`) must be captured WHOLE. Capturing only `EOF` would never
    match the real `EOF-MSG` terminator, skip to end of input, and hide the real merge on the line
    AFTER the heredoc — the exact bypass this gate prevents (Codex review on the follow-up)."""
    out, _err, code = _run(
        "cat <<EOF-MSG\nbody mentions gh pr merge\nEOF-MSG\ngh pr merge 1 --admin", monkeypatch
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_punctuated_heredoc_delimiter_body_is_still_skipped(monkeypatch):
    """The whole-word delimiter capture must still skip the body of a punctuated-delimiter heredoc:
    a `gh pr merge` inside the body is document text and must ALLOW."""
    out, _err, code = _run("cat <<EOF-MSG\ngh pr merge 1 in body\nEOF-MSG", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "opener",
    [
        "<<\\EOF",  # escaped delimiter — terminates on `EOF`
        '<<E"OF"',  # mixed quoting within the word — terminates on `EOF`
        "<<'EOF'",  # fully single-quoted
    ],
)
def test_quote_removed_heredoc_delimiter_does_not_hide_a_later_merge(opener, monkeypatch):
    r"""The delimiter word is quote-removed the way the shell does (`\EOF`, `E"OF"`, `'EOF'` all
    terminate on `EOF`). Capturing the raw form would never match the `EOF` terminator, skip the
    body to end of input, and hide the real merge after the heredoc (Codex review on the follow-up)."""
    out, _err, code = _run(f"cat {opener}\nbody\nEOF\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "(( 1 << 2 ))\ngh pr merge 1 --admin",  # arithmetic left-shift, NOT a heredoc opener
        "echo $((1 << 2))\ngh pr merge 1 --admin",  # arithmetic expansion
    ],
)
def test_arithmetic_left_shift_is_not_a_heredoc(command, monkeypatch):
    """`<<` inside `(( … ))` / `$(( … ))` is a left-shift operator, not a heredoc. Mis-reading it as
    a heredoc opener and skipping to a never-found terminator would swallow the real merge on the
    next line and ALLOW it. Must BLOCK (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo $(true)\ngh pr merge 1 --admin",  # prior line ends in `)` from command substitution
        "(echo x)\ngh pr merge 1 --admin",  # prior line ends in `)` from a subshell
    ],
)
def test_prior_line_ending_in_paren_does_not_glue_the_separator(command, monkeypatch):
    """The inserted `;` must be a standalone split point. If a prior line ends in `)`, gluing it to
    `;` yields a `);` punctuation run that `_split_segments` does not treat as a separator, keeping
    the merge in the previous segment and ALLOWING it. Must BLOCK (Codex review on the follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
