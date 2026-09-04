#!/usr/bin/env python3
"""Every place that tells a caller how to satisfy the review gate must print the SAME
runnable command.

This repo blocks commits from two independent gates (the agents-hooks pre-bash hook and
the global git-hook dispatcher) and explains the fix in three more places (this hook's
README, the anti-wedge skill, AGENTS.md). None of them can share a runtime value: a shell
hook cannot import a Python constant, and prose cannot interpolate one. So they drift —
and drift here is not cosmetic. The incident that produced these tests was exactly this:
one file still recommended `review --uncommitted`, a form review-cli had removed, and two
detached agents followed it into a wall and died there.

The check is deliberately dumb — substring presence, not parsing — because the failure it
guards against is a stale string, not a malformed one. When review-cli's surface changes,
update `_REVIEW_CLI_INVOCATION` in the hook; this test then names every file that still
disagrees.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_review_invocation_is_consistent.py -q
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HOOK = _ROOT / "agent-hooks" / "require-review-before-commit" / "require_review.py"

_spec = importlib.util.spec_from_file_location("require_review", _HOOK)
assert _spec and _spec.loader
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

# The canonical string, owned by the hook. Everything else is checked against it.
CANONICAL = rr._REVIEW_CLI_INVOCATION

# Files that tell a human or an agent how to satisfy the gate. Each must contain the
# canonical invocation verbatim. A file may say more (a `-C <repo>` variant, an example
# with a real task code); it may not say LESS than the runnable form.
_CALLERS = (
    "git-hooks/global-dispatcher/hooks/review-gate",
    "agent-hooks/require-review-before-commit/README.md",
    # The installed descriptor's `description` is catalog metadata a harness or a reader
    # consults to learn what the gate wants; it carried "review the uncommitted diff" — an
    # unstaged review writes no marker and leaves the commit blocked (codex finding on #510).
    "agent-hooks/require-review-before-commit/require-review-before-commit.pre-bash.json",
    "skills/universal/anti-wedge-review/SKILL.md",
    "AGENTS.md",
    # The CI review script and its README are not gates, but they are read by someone
    # trying to review before a commit, and they carried the dead `codex exec review
    # --uncommitted` form long after it stopped existing (codex finding). Anything that
    # tells a reader how to get a review belongs under this coverage.
    "ci/ai-review/README.md",
    "ci/ai-review/ai-review.sh",
)


# A line carrying one of these is showing a command as something NOT to do — a mechanism
# that wedges, or a shape that writes no marker — so the flags on it are beside the point.
# Kept literal and short on purpose: every addition here is a hole in the check.
_COUNTER_EXAMPLE_MARKERS = ("FORBIDDEN", "NOT for a commit", "| NO |")


def _lines_with_an_incomplete_review_diff(body: str) -> list[str]:
    """Lines that show a `review diff` INVOCATION missing `--staged` or `--task`.

    Scanning for the literal `review diff --staged` was not enough (Fable finding): it
    only sees commands that already carry the flag, so a reinstated `review diff --task
    <CODE> -C /repo` — the exact pre-change instruction this work removed — slips through
    with no `--staged` and writes no marker. So anchor on `review diff` and require BOTH.

    "Invocation" has to be distinguished from a mention, or every sentence that discusses
    the command ("an unstaged `review diff` leaves the marker alone") becomes an offender
    and the check gets deleted by the next person it annoys. The distinction used here is
    the next token: a command is followed by a FLAG, prose is followed by a word.

    A line may omit a flag if it TALKS about that flag (what a deliberate negative example
    does — "`review diff --staged` with no `--task` → exits 2" — and what a stale
    copy-paste never does), or if it labels itself a counter-example with one of
    `_COUNTER_EXAMPLE_MARKERS`. Those markers are deliberately few and literal: this file
    documents wedges by showing them, and a check that flagged its own illustrations would
    be deleted by the first person it annoyed.
    """
    offenders = []
    for line in body.splitlines():
        if any(marker in line for marker in _COUNTER_EXAMPLE_MARKERS):
            continue
        for follower in re.findall(r"review diff\s+(\S+)", line):
            if not follower.strip("`'\"").startswith("-"):
                continue  # prose about the command, not a command
            if "--staged" in line and ("--task" in line or "REVIEW_TASK_CODE" in line):
                continue
            offenders.append(line.strip())
            break
    return offenders


def _incomplete_invocations_in(text: str) -> list[str]:
    """Every `review diff` INVOCATION in `text` that is missing `--staged` or `--task`.

    The per-line helper above needs line context to honour counter-example markers, which
    makes it useless on a single-line message: one correct command on that line vouches
    for every broken one beside it. This one scans each occurrence independently, taking
    the command to run until the closing backtick, a comma, or the end of the text.
    """
    incomplete = []
    for match in re.finditer(r"review diff(?P<rest>[^`,.]*)", text):
        rest = match.group("rest")
        if not rest.strip().startswith("-"):
            continue  # prose about the command, not a command
        if "--staged" not in rest or ("--task" not in rest and "REVIEW_TASK_CODE" not in rest):
            incomplete.append(("review diff" + rest).strip())
    return incomplete


def _read(rel: str) -> str:
    path = _ROOT / rel
    assert path.exists(), f"{rel} moved or was deleted — update this test's caller list"
    return path.read_text(encoding="utf-8")


def test_canonical_invocation_is_actually_runnable():
    """The constant itself must carry the pieces without which the command exits 2 and
    reviews nothing — the failure mode that makes a stale instruction so expensive."""
    assert "review diff" in CANONICAL
    assert "--staged" in CANONICAL, "an unstaged review does not satisfy the gate"
    assert "--task" in CANONICAL, "`review diff` exits 2 without --task/REVIEW_TASK_CODE"


@pytest.mark.parametrize("rel", _CALLERS)
def test_every_gate_instruction_prints_the_canonical_invocation(rel: str):
    """A caller that prints a command missing `--staged` or `--task` sends the reader to
    a run that reviews nothing and leaves the commit blocked — the wedge, restarted."""
    body = _read(rel)
    assert CANONICAL in body, (
        f"{rel} does not contain the canonical invocation {CANONICAL!r}. "
        "Either it drifted, or the constant changed and this file was not updated with it."
    )


@pytest.mark.parametrize("rel", _CALLERS)
def test_no_caller_shows_a_staged_review_example_without_a_task_code(rel: str):
    """The canonical-string check above only proves the right command appears SOMEWHERE
    in a file; it says nothing about the other command-shaped strings around it. A stale
    `review diff --staged` example two paragraphs down is just as runnable-looking and
    exits 2 just the same (codex finding, iteration 6 — the README carried exactly that
    in its illustration of the mtime gap).

    A line may drop `--task` only if it TALKS about `--task`/`REVIEW_TASK_CODE` — which is
    what a deliberate negative example ("`review diff --staged` with no `--task` → exits
    2") does, and what a stale copy-paste never does."""
    offenders = _lines_with_an_incomplete_review_diff(_read(rel))
    assert not offenders, (
        f"{rel} shows an incomplete `review diff` invocation on:\n  "
        + "\n  ".join(offenders)
        + "\nWithout --staged it writes no marker; without --task it exits 2 and reviews "
        "nothing. Either way the reader is left with a blocked commit."
    )


def test_no_caller_recommends_the_dead_uncommitted_form():
    """`review --uncommitted` is the exact command that killed two detached agents: the
    flag no longer exists, so the advice fails and the caller is left with a blocked
    commit and no explanation. It must not come back anywhere a caller reads."""
    # Scoped to the files that INSTRUCT a caller. The hook's own module docstring names
    # the dead form deliberately, to record why it must never come back — narrating the
    # incident is the opposite of recommending it.
    for rel in _CALLERS:
        body = _read(rel)
        assert "review --uncommitted" not in body, (
            f"{rel} recommends `review --uncommitted`, a form review-cli removed"
        )


def test_block_message_prints_the_runnable_command(capsys):
    """The pre-bash hook's own block message is the single highest-stakes copy: it is what
    a stuck agent reads at the exact moment it is blocked, and it is the one that carried
    the dead command during the incident. The shell gate's equivalent message is pinned by
    tests/test_global_review_gate.py; this pins its counterpart."""
    rr._block()
    # ONE readouterr() call: it drains and resets BOTH buffers, so calling it twice
    # returns the second one empty and silently discards half the message.
    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert CANONICAL in printed, printed
    assert "touch" in printed, "the message must still warn off the hand-forged marker"
    # And a per-INVOCATION scan, not the per-line one the files get. The block message is
    # a single long line, so a line-based check is vacuous here: the canonical command
    # sitting on that same line would mask any incomplete example added beside it (Fable
    # finding). Each `review diff …` occurrence is therefore checked on its own.
    assert not _incomplete_invocations_in(printed), printed
