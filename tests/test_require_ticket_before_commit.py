"""Tests for the require-ticket-before-commit agent-hook (pre-bash, strict-by-default).

This is the CI-collected mirror of the hook's own `test_require_ticket.py` (which lives
beside the script under `agent-hooks/` and is not picked up by `pytest tests/`). It pins
the contract the task-cli Docker test and the orchestration swarm depend on:

  - STRICT BY DEFAULT: a no-ticket, non-chore `git commit` BLOCKS (exit 10, decision "block")
    with no env set — the inversion this hook now ships.
  - the block message LEADS with the stable marker `[require-ticket] BLOCKED: no ticket
    reference`, so an external check can assert on one fixed string.
  - EVERY accepted ticket-reference form PASSES (Closes #N / Fixes #N / #N / a KEY-NUM
    task-cli id / a `task:` form / a `[ticket: …]` trailer) — a legit commit is never wedged.
  - the two deliberate escapes work: a `[skip-ticket: <reason>]` message trailer and an
    inline `REQUIRE_TICKET_SKIP=1 git commit …` — and a blank reason / falsey value / a
    sibling-scoped assignment do NOT bypass.
  - `REQUIRE_TICKET_STRICT=0` opts back to warn-only (allow with advisory).

The hook reads only the command + cwd from the stdin event (the cwd is an isolated empty
temp dir so branch detection finds no incidental ticket pattern). Pure parse — no temp repo.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_require_ticket_before_commit.py -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "require-ticket-before-commit"
    / "require_ticket_before_commit.py"
)
_spec = importlib.util.spec_from_file_location("require_ticket", _HOOK)
assert _spec and _spec.loader
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


def _run(command: str, *, strict: bool | None = None, env_extra: dict | None = None) -> dict:
    """End-to-end: feed the stdin JSON event to the script; return {code, decision, message}.

    strict=None leaves REQUIRE_TICKET_STRICT UNSET (the real default → strict block);
    True sets `=1`; False sets `=0` (the warn-only opt-out). cwd is an isolated empty dir.
    """
    import os

    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_SKIP", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    if strict is True:
        env["REQUIRE_TICKET_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TICKET_STRICT"] = "0"
    if env_extra:
        env.update(env_extra)
    with tempfile.TemporaryDirectory() as isolated:
        event = json.dumps({"args": {"command": command}, "cwd": isolated})
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,
            timeout=10,
        )
    payload = json.loads((proc.stdout or "").strip())
    return {"code": proc.returncode, "decision": payload["decision"], "message": payload.get("message", "")}


# ── STRICT BY DEFAULT ─────────────────────────────────────────────────────────────────────────


def test_no_ticket_blocks_by_default():
    r = _run('git commit -m "feat: add export"')
    assert r["code"] == rt.BLOCK_EXIT_CODE == 10
    assert r["decision"] == "block"


def test_block_message_leads_with_stable_marker():
    r = _run('git commit -m "feat: add export"')
    assert r["decision"] == "block"
    assert rt.BLOCK_MARKER == "[require-ticket] BLOCKED: no ticket reference"
    assert r["message"].startswith(rt.BLOCK_MARKER)


def test_bare_fix_blocks_by_default():
    # `fix:` is a bugfix (not exempt) and the subject has no ref.
    r = _run('git commit -m "fix: null deref"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_strict_disabled_warns_but_allows():
    r = _run('git commit -m "feat: add export"', strict=False)
    assert r["code"] == 0
    assert r["decision"] == "allow"


def test_warn_mode_message_omits_block_marker():
    # The stable marker must appear ONLY on a real block — never on a warn-mode allow, or an
    # external grep-for-marker check would false-positive on an allowed commit.
    r = _run('git commit -m "feat: add export"', strict=False)
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]


# ── EVERY VALID REFERENCE FORM PASSES ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("subject", [
    "feat: add export\n\nCloses #123",   # Closes #N
    "fix: crash\n\nFixes #4",            # Fixes #N
    "feat: add export #88",             # bare #N
    "feat: add export ENG-456",         # KEY-NUM task-cli/Linear id
    "feat: add export task:ABC-12",     # task: form
    "feat: add export [ticket: PROJ-9]",       # [ticket: id]
    "feat: add export [ticket: backfill-orders]",  # [ticket: slug] (non-numeric task-cli id)
])
def test_valid_ref_forms_pass(subject):
    r = _run(f'git commit -m {subject!r}')
    assert r["code"] == 0, f"{subject!r} should pass but exit={r['code']} ({r['message']})"
    assert r["decision"] == "allow"


# ── THE TWO DELIBERATE ESCAPES ──────────────────────────────────────────────────────────────────


def test_skip_ticket_trailer_allows():
    r = _run('git commit -m "feat: x [skip-ticket: one-off backfill]"')
    assert r["code"] == 0
    assert r["decision"] == "allow"


def test_blank_skip_ticket_reason_still_blocks():
    r = _run('git commit -m "feat: x [skip-ticket: ]"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_inline_skip_env_allows():
    r = _run('REQUIRE_TICKET_SKIP=1 git commit -m "feat: x"')
    assert r["code"] == 0
    assert r["decision"] == "allow"


def test_inline_skip_falsey_still_blocks():
    r = _run('REQUIRE_TICKET_SKIP=0 git commit -m "feat: x"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_inline_skip_on_sibling_does_not_bypass():
    r = _run('REQUIRE_TICKET_SKIP=1 echo hi && git commit -m "feat: x"')
    assert r["code"] == 10
    assert r["decision"] == "block"


# ── -F MESSAGE SOURCE: file readable, stdin (-F -) fail-CLOSED with a hint ───────────────────────
# #100 made `-F <file>` readable; #102 first handled the `-F -` (stdin) gap by failing OPEN, but
# agent-tools#104 reverses that: git streams a `-F -` message on its OWN stdin, which a PreToolUse
# hook cannot read, and silently allowing it made `-F -` a free bypass of the ticket gate. So for
# `-F -` ONLY the gate now FAILS CLOSED (block, exit 10, the marker) with an actionable hint —
# unless a readable escape (`[skip-ticket]` / `REQUIRE_TICKET_SKIP=1`) or warn-only mode applies.


# Every readable `-F`/`--file` SPELLING (separate, glued, long, `=`) must actually READ the file and
# gate on its ticket — the stdin fail-closed is ONLY for `-F -`, never for a real file in any spelling.
@pytest.mark.parametrize("spelling", ["-F {p}", "-F{p}", "--file {p}", "--file={p}"])
def test_file_spellings_with_ticket_allow(tmp_path, spelling):
    msg = tmp_path / "COMMIT_MSG.txt"
    msg.write_text("feat: add export\n\nCloses #321\n", encoding="utf-8")
    r = _run(f"git commit {spelling.format(p=msg)}")
    assert r["code"] == 0, f"{spelling!r} with Closes #321 must read the file and allow ({r['message']})"
    assert r["decision"] == "allow"


@pytest.mark.parametrize("spelling", ["-F {p}", "-F{p}", "--file {p}", "--file={p}"])
def test_file_spellings_without_ticket_block(tmp_path, spelling):
    msg = tmp_path / "COMMIT_MSG.txt"
    msg.write_text("feat: add export\n", encoding="utf-8")
    r = _run(f"git commit {spelling.format(p=msg)}")
    assert r["code"] == 10, f"{spelling!r} readable file with no ticket must still block ({r['message']})"
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    "git commit -F -",       # separate stdin sentinel
    "git commit -F-",        # glued
    "git commit --file -",   # long form
    "git commit --file=-",   # long form, glued
])
def test_dash_F_stdin_fails_closed_with_hint(command):
    # agent-tools#104: a `-F -` stdin message is unreadable, so rather than let it bypass the gate it
    # now BLOCKS (exit 10, the marker) with an ACTIONABLE hint naming the readable ways + escapes.
    r = _run(command)
    assert r["code"] == 10, f"{command!r} streams the message on stdin (unreadable) — must fail-closed"
    assert r["decision"] == "block"
    assert r["message"].startswith(rt.BLOCK_MARKER)
    msg = r["message"]
    assert "stdin" in msg.lower(), "the hint must name the unreadable-stdin cause"
    assert "-m" in msg and "-F <file>" in msg, "the hint must name the readable ways to satisfy it"
    assert "branch" in msg.lower(), "the hint must mention the branch-name path"
    assert "REQUIRE_TICKET_SKIP=1" in msg, "the hint must name the escape that actually works"
    # the `[skip-ticket: …]` trailer can't work for `-F -` (it lives in the unreadable stdin
    # message) — the hint must not falsely promise it.
    assert "skip-ticket" not in msg.lower(), "the hint must not offer the trailer escape for `-F -`"


@pytest.mark.parametrize("command", [
    "REQUIRE_TICKET_SKIP=1 git commit -F -",
    "REQUIRE_TICKET_SKIP=1 git commit --file=-",
])
def test_dash_F_stdin_inline_skip_escape_still_works(command):
    # the deliberate inline escape still lets a genuine no-ticket `-F -` commit through — it's read
    # from the command env, not git's unreadable stdin (agent-tools#104).
    r = _run(command)
    assert r["code"] == 0, f"{command!r} carries REQUIRE_TICKET_SKIP=1 — must allow"
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]


def test_dash_F_stdin_warn_only_downgrades_to_allow():
    # REQUIRE_TICKET_STRICT=0 downgrades the `-F -` block to a warn-only allow (no marker leaked).
    r = _run("git commit -F -", strict=False)
    assert r["code"] == 0
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]
    assert "stdin" in r["message"].lower()


def test_dash_F_stdin_on_ticket_branch_allows(tmp_path):
    # A `-F -` (unreadable stdin message) commit is still ticketed by its BRANCH name, read via
    # `git -C <repo>` — same as the editor-commit path. The stdin block runs AFTER branch detection,
    # so this legit workflow passes rather than false-blocking (agent-tools#104 review finding).
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feature/ENG-42-export", str(repo)], check=True)
    r = _run(f"git -C {repo} commit -F -", strict=True)
    assert r["code"] == 0, f"branch ENG-42 should satisfy the gate even for `-F -` ({r['message']})"
    assert r["decision"] == "allow"


def test_dash_F_stdin_on_non_ticket_branch_still_blocks(tmp_path):
    # control: a `-F -` on a branch WITHOUT a ticket id still fails-closed — no branch silently
    # defeats the gate.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    r = _run(f"git -C {repo} commit -F -", strict=True)
    assert r["code"] == 10
    assert r["decision"] == "block"


# ── EXEMPTIONS still hold under the strict default ──────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    'git commit -m "chore: bump lockfile"',
    'git commit -m "docs(readme): fix typo"',
    'git commit -m "wip: spike"',
    "git commit --amend --no-edit",
    "git status",
])
def test_exempt_and_non_commit_allowed(command):
    r = _run(command)
    assert r["code"] == 0
    assert r["decision"] == "allow"


# ── OVER-MATCH REGRESSION (agent-tools#97) ──────────────────────────────────────────────────────
# The gate must fire ONLY on a real `git commit` invocation — never when the words "git"/"commit"
# merely appear as a substring/argument/message-body of some OTHER command. The old raw regex
# `\bgit\b.*\bcommit\b` over the RAW command string blocked every benign command that mentioned both
# words (a LIVE false positive: a subagent's `gh issue create` whose body said "git commit").


@pytest.mark.parametrize("command", [
    # the LIVE repro: an issue body containing the words "git commit"
    'gh issue create --title "fix gate" --body "we should git commit only on a real commit"',
    'echo "git commit"',                       # echo is not a commit
    "git log --grep=commit",                   # git log, not git commit (glued value)
    "git log --grep commit -n 5",              # git log, not git commit (separate value)
    'grep -rn "git commit" src/',              # a grep whose pattern says "commit"
    "git config commit.gpgsign true",          # mentions commit, not authoring one
    "git help commit",                         # help for the commit subcommand
    "git commit-graph write",                  # a DIFFERENT subcommand
    'tg "I will git commit the fix once review passes"',  # a status report ABOUT a commit
])
def test_mentioning_commit_is_not_gated(command):
    r = _run(command)
    assert r["code"] == 0, f"{command!r} is not a git commit — must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_real_commit_after_a_mentioning_sibling_still_blocks():
    # `echo "git commit" && git commit -m feat` — the SECOND segment is a real, ticketless commit.
    r = _run('echo "git commit" && git commit -m "feat: add export"')
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    'env FOO=bar git commit -m "feat: add export"',          # env-prefixed
    'FOO=bar git commit -m "feat: add export"',              # bare inline VAR=value
    'sudo git commit -m "feat: add export"',                 # sudo
    'sudo -u git git commit -m "feat: add export"',          # sudo -u <user named git>
    'runuser -u git -- git commit -m "feat: add export"',    # runuser
    'timeout 60 git commit -m "feat: add export"',           # timeout (operand-drop)
    '/usr/bin/git commit -m "feat: add export"',             # path-qualified git
])
def test_wrapped_no_ticket_commit_still_blocks(command):
    r = _run(command)
    assert r["code"] == 10, f"{command!r} is a real wrapped commit — must block, got exit={r['code']}"
    assert r["decision"] == "block"


# ── CLUSTERED SHORT FLAG `-am` (agent-tools#109) ─────────────────────────────────────────────────
# `git commit -am "…"` is `-a` + `-m "…"`; the message-extraction path must de-cluster combined short
# option groups the way git's commit parser does (the FIRST value letter `m`/`F` wins — glued tail or
# next token). The pre-fix code matched only `-m`/`-mMSG`/`-F`/`-FPATH`, so `-am`/`-aF`/`-amMSG` fell
# through with an EMPTY message → a ticketed `-am "Closes #5"` false-BLOCKED and a `-am "chore: …"`
# LOST its exemption. Same clustered-short-flag class block-no-verify fixed in #36–#40.


def test_am_with_ticket_allows():
    # THE issue #109 repro: a ticket in a `-am` message must be detected → ALLOW.
    r = _run('git commit -am "Closes #5 fix"')
    assert r["code"] == 0, f"a ticketed -am commit must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_am_without_ticket_blocks():
    r = _run('git commit -am "no ticket"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_am_chore_exemption_is_preserved():
    # the second half of the bug: an empty message lost the `chore:` exemption too.
    r = _run('git commit -am "chore: cleanup"')
    assert r["code"] == 0, f"a chore: -am commit is exempt and must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_am_glued_ticket_allows():
    # `-amENG-7` — the message is glued onto the cluster.
    r = _run("git commit -amENG-7")
    assert r["code"] == 0
    assert r["decision"] == "allow"


@pytest.mark.parametrize("command", [
    'git commit -am "no ticket [skip-ticket: deliberate]"',  # `[skip-ticket: …]` trailer escape
    'REQUIRE_TICKET_SKIP=1 git commit -am "no ticket"',      # inline-env escape
])
def test_am_escapes_still_work(command):
    r = _run(command)
    assert r["code"] == 0, f"{command!r} carries a deliberate escape — must allow"
    assert r["decision"] == "allow"


@pytest.mark.parametrize("sep", [" ", ""])  # `-aF <file>` (next token) and `-aF<file>` (glued)
def test_am_aF_file_with_ticket_allows(tmp_path, sep):
    # `-aF` clustered — the message file must be read and its ticket detected, both spellings.
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("feat: big change\n\nCloses #99\n")
    r = _run(f"git commit -aF{sep}{msg_file}")
    assert r["code"] == 0, f"a -aF file with a ticket must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_amF_is_a_message_not_a_file_block(tmp_path):
    # `-amF` is message "F" (git glues F onto -m), NOT a `-F` file read → no ticket → BLOCK.
    r = _run("git commit -amF")
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    "tar -amF archive.tar",          # a clustered short flag on a NON-git command
    "ls -la",                        # benign cluster, not a commit
    'grep -rn "git commit -am" src/',  # a grep whose pattern contains `-am`
])
def test_benign_clustered_flag_non_commit_allowed(command):
    # the #37/#40 over-block cautionary tale: de-clustering must not over-match a non-commit command.
    r = _run(command)
    assert r["code"] == 0, f"{command!r} is not a git commit — must allow"
    assert r["decision"] == "allow"


def test_am_amend_value_still_blocks():
    # `git commit -am "--amend"` authors a real ticketless commit (message "--amend") → BLOCK; the
    # `--amend` is the -m VALUE, not a real skip flag.
    r = _run('git commit -am "--amend"')
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    'git commit -am "-F"',          # `-am "-F"`: message is "-F", the value is not a real -F flag
    "git commit -am -F -",          # `-am` takes "-F" as the message; `-` is a pathspec, NOT stdin
    "git commit -m -F -",           # short separate form
    "git commit --message -F -",    # LONG separate form (re-review #1)
])
def test_message_with_dash_F_looking_value_is_not_a_stdin_block(command):
    # agent-tools#109 review #1: a `-m`/`-am`/`--message` whose VALUE looks like `-F` must NOT be
    # re-parsed as a real `-F -` stdin read (which would false-BLOCK with the stdin hint). The
    # message "-F" carries no ticket so the commit still BLOCKS — but with the ordinary no-ticket
    # marker, NOT the stdin hint; the point is the gate did not misclassify it as a stdin commit.
    r = _run(command)
    assert r["code"] == 10, command
    assert r["decision"] == "block"
    assert "on git's stdin" not in r["message"], f"must not be the stdin hint: {command!r}"


@pytest.mark.parametrize("command", ["git commit -t -F -", "git commit -aC -F -"])
def test_reuse_template_flag_value_is_not_a_stdin_block(command):
    # agent-tools#109 re-review #2: a SEPARATE `-C`/`-t` takes its next token as a mandatory value —
    # `git commit -t -F -` is `-t` template "-F", NOT a `-F -` stdin read. Must not be the stdin hint.
    r = _run(command)
    assert r["code"] == 10
    assert "on git's stdin" not in r["message"], command


def test_reuse_message_cluster_is_not_a_dash_m_message():
    # agent-tools#109 review #2: `git commit -Cm` is `-C m` (reuse commit "m"), NOT a `-m` message.
    # With no readable message and no ticket, it blocks — and must NOT extract a phantom message.
    assert rt.commit_message_from_argv(["-Cm"]) == ""
    assert rt.commit_message_from_argv(["-um"]) == ""
    assert rt.commit_message_from_argv(["-Sm"]) == ""
    # SEPARATE `-C`/`-c`/`-t` swallow their next token too — no phantom message from the `-m`.
    assert rt.commit_message_from_argv(["-C", "-m", "x"]) == ""
    assert rt.commit_message_from_argv(["-t", "-m", "x"]) == ""


@pytest.mark.parametrize("command", [
    "git commit -S -m 'Closes #5'",   # -S optional keyid (no value) then a REAL -m
    "git commit -u -m 'Fixes ABC-7'",  # -u optional untracked mode then a REAL -m
])
def test_optional_arg_flag_then_separate_message_with_ticket_allows(command):
    # agent-tools#109 re-review #3: `-S`/`-u` have an OPTIONAL only-glued value, so a separate
    # `-S -m …` must still read the `-m` ticket → ALLOW (they must NOT consume the next token).
    r = _run(command)
    assert r["code"] == 0, f"{command!r} carries a ticket in a real -m — must allow"
    assert r["decision"] == "allow"
