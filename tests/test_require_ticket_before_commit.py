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


# ── -F MESSAGE SOURCE: file readable, stdin (-F -) fail-open ─────────────────────────────────────
# #100 made `-F <file>` readable; #101 closes the remaining `-F -` (stdin) gap — git streams that
# message on its OWN stdin, which a PreToolUse hook cannot read, so for `-F -` ONLY the gate must
# fail-OPEN (allow) with a logged note rather than false-block a possibly-ticketed commit.


# Every readable `-F`/`--file` SPELLING (separate, glued, long, `=`) must actually READ the file and
# gate on its ticket — the stdin fail-open is ONLY for `-F -`, never for a real file in any spelling.
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
def test_dash_F_stdin_fails_open_with_log(command):
    r = _run(command)
    assert r["code"] == 0, f"{command!r} streams the message on stdin (unreadable) — must fail-open"
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]
    assert "stdin" in r["message"].lower(), "the unreadable-message bypass must be logged/visible"


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
