"""Tests for the decision-request-format agent-hook (pre-bash, advisory — never blocks).

Covers: TRIGGER detection (`tg --tag decision`, parsed not raw-matched, through wrappers and
pipelines), MARKER checking (missing → advisory message; complete → silent allow), NON-TRIGGER
cases (other tag / no tag / non-tg / flag-inside-a-string), the ESCAPE hatch (env + inline
sentinel), and FAIL-OPEN on a malformed event. Every path returns exit 0; the signal under test
is whether the `allow` carries an advisory `message`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_decision_request_format.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "decision-request-format"
    / "decision_request_format.py"
)
_spec = importlib.util.spec_from_file_location("decision_request_format", _HOOK)
assert _spec and _spec.loader
drf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drf)


def _run(command, monkeypatch, *, env: dict | None = None, raw_event=None):
    event = raw_event if raw_event is not None else {"args": {"command": command}}
    payload = event if isinstance(event, str) else json.dumps(event)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("ALLOW_RAW_DECISION_REQUEST", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = drf.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _message(out: str) -> str | None:
    return json.loads(out).get("message")


# A body that mentions none of the three dimensions — the bare "A or B?".
_BARE = "should we go with A or B?"
# A body that hits all three markers (context + options/pros-cons + recommendation).
_COMPLETE = (
    "Context: the loader in app.py:42. Options: keep sync (simple, blocks the UI) vs "
    "async (faster, more complex). Recommendation: go async."
)


# ── TRIGGER + missing markers → advisory ────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    f'tg --tag decision "{_BARE}"',
    f'tg --tag=decision "{_BARE}"',
    f'tg --tag decision --title "{_BARE}" "more body"',
    f'env TG_AI_MODEL=claude tg --tag decision "{_BARE}"',
    f'TG_AI_MODEL=claude tg --tag decision "{_BARE}"',   # bare assignment (skill's OWN form)
    f'A=1 B=2 tg --tag decision "{_BARE}"',              # several bare assignments
    f'TG_AI_MODEL=claude timeout 30 tg --tag decision "{_BARE}"',  # assignment + wrapper
    f'timeout 30 tg --tag decision "{_BARE}"',
    f'timeout -s KILL 30 tg --tag decision "{_BARE}"',   # wrapper flag w/ separate value
    f'nice -n 10 tg --tag decision "{_BARE}"',           # nice -n with a separate value
    f'env -u FOO tg --tag decision "{_BARE}"',           # env -u FOO (flag + its value)
    f'build_msg | tg --tag decision "{_BARE}"',
    f'/usr/local/bin/tg --tag decision "{_BARE}"',
])
def test_missing_markers_emits_advisory(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    msg = _message(out)
    assert msg is not None and "self-check" in msg


# The advisory message names the missing markers as a `missing <A>, <B> (of the three…` list,
# then goes on to a static reminder. A test that just asserts `missing_label in msg` is vacuous
# — "Context"/"Options"/"Recommendation" all appear somewhere in the static text regardless of
# what was detected. We assert against the DYNAMIC list only (the part between "missing " and
# the "(of the three" qualifier): the present markers must NOT appear there, the absent one must.
_MISSING_LIST = re.compile(r"missing ([^(]+)\(of the three")


def _named_missing(msg: str) -> set[str]:
    m = _MISSING_LIST.search(msg)
    assert m, f"advisory did not contain a `missing …` list: {msg!r}"
    return {part.strip() for part in m.group(1).split(",")}


@pytest.mark.parametrize("body,absent,present", [
    ("Options: A (fast) vs B (safe). Recommendation: A.", "Context", ("Options", "Recommendation")),
    ("Context: app.py. Recommendation: pick A.", "Options", ("Context", "Recommendation")),
    ("Context: app.py. Options: A (fast) vs B (safe).", "Recommendation", ("Context", "Options")),
])
def test_each_missing_marker_named(body, absent, present, monkeypatch):
    out, _, _ = _run(f'tg --tag decision "{body}"', monkeypatch)
    msg = _message(out)
    assert msg is not None
    named = _named_missing(msg)
    assert absent in named, f"the absent marker {absent!r} should be listed; got {named}"
    for marker in present:
        assert marker not in named, f"the present marker {marker!r} must not be listed; got {named}"


def test_missing_markers_unit_is_dynamic():
    """Directly exercise the detector so the marker logic is verified, not just the message."""
    assert drf._missing_markers("nothing here") == ["Context", "Options", "Recommendation"]
    assert drf._missing_markers(_COMPLETE) == []
    assert drf._missing_markers("Options: A vs B. Recommendation: A.") == ["Context"]


@pytest.mark.parametrize("ref", ["app.py:42", "src/loader.ts:128"])
def test_file_line_ref_counts_as_context(ref):
    # The skill's format point 1 prescribes a `file:line` code ref AS the Context. A body that
    # gives a ref instead of the literal word "context" must NOT be flagged missing Context.
    assert "Context" not in drf._missing_markers(f"{ref} — Options: A vs B. Recommendation: A.")


@pytest.mark.parametrize("nonref", ["localhost:8080", "12:30", "a:5"])
def test_bare_host_port_or_time_is_not_context(nonref):
    # A bare `host:port` or `HH:MM` must NOT be mistaken for a file ref — otherwise the most
    # valuable marker (Context) gets silenced for a body that has no real context. The `.ext`
    # requirement in the regex is what makes this hold.
    assert "Context" in drf._missing_markers(f"{nonref} — should we pick A or B?")


def test_second_tg_in_pipeline_is_inspected(monkeypatch):
    # The FIRST `tg` segment is a non-decision send (--tag report) → returns None; the loop must
    # continue to the SECOND `tg --tag decision` and inspect IT. This is the only non-trivial
    # segment-walking branch — without the continuation, the decision request would be missed.
    out, _, _ = _run(
        f'tg --tag report "build green" ; tg --tag decision "{_BARE}"', monkeypatch
    )
    assert _message(out) is not None


def test_empty_body_with_decision_tag_advises(monkeypatch):
    # `tg --tag decision` with no positional text and no --title → body == "" → all three markers
    # missing → advisory fires (no crash on the empty string).
    out, _, code = _run('tg --tag decision', monkeypatch)
    assert code == 0
    assert _message(out) is not None


# ── TRIGGER + complete body → silent allow ──────────────────────────────────────────────

def test_complete_body_allows_silently(monkeypatch):
    out, _, code = _run(f'tg --tag decision "{_COMPLETE}"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert _message(out) is None


# ── NON-TRIGGER → plain allow, no message ───────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    'tg --tag report "build green"',                       # a different tag
    'tg --tag problem "the deploy is down"',               # a different tag
    'tg "just a status note"',                             # no tag at all
    'echo "use --tag decision next time"',                 # the flag inside an echo string
    'tg "remember to --tag decision later"',               # body text, no actual flag
    'git commit -m "add tg --tag decision hook"',          # the flag inside a commit message
    'ls -la /opt/tg/decision',                             # a path that merely contains tg
])
def test_non_trigger_plain_allow(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    assert _message(out) is None


def test_value_flag_value_not_treated_as_body(monkeypatch):
    # A value-flag's VALUE must not be collected as the message body. Here the ONLY text that
    # would satisfy markers sits in --reply-to's value; the real body is a bare "A or B?". With
    # --tag decision present, the advisory must STILL fire (proving the value was skipped, not
    # mistaken for the body that has the markers). A vacuous version without --tag decision
    # would return None regardless of value-flag handling.
    out, _, _ = _run(
        'tg --tag decision --reply-to "Context: Options: Recommendation:" "A or B?"', monkeypatch
    )
    msg = _message(out)
    assert msg is not None  # the markers in --reply-to's value did NOT silence the advisory


def test_value_flag_before_real_body_with_decision_tag(monkeypatch):
    # `--photo x.png` value skipped; the real positional body carries all markers → silent allow.
    out, _, _ = _run(
        'tg --tag decision --photo /tmp/x.png '
        '"Context: app.py. Options: A vs B. Recommendation: A."', monkeypatch
    )
    assert _message(out) is None


def test_title_value_participates_in_marker_check(monkeypatch):
    # The body is assembled from --title PLUS positionals, so markers split across the two must
    # all count. Here Context lives in --title and Options/Recommendation in the positional →
    # a complete body → silent allow. (A naive impl that ignored --title would flag Context.)
    out, _, _ = _run(
        'tg --tag decision --title "Context: app.py:42" "Options: A vs B. Recommendation: A."',
        monkeypatch,
    )
    assert _message(out) is None
    # And the inverse: markers ONLY in --title, a bare positional → still complete via --title.
    out2, _, _ = _run(
        'tg --tag decision --title '
        '"Context: app.py. Options: A vs B. Recommendation: A." "ping"',
        monkeypatch,
    )
    assert _message(out2) is None


# ── ESCAPE hatch ────────────────────────────────────────────────────────────────────────

def test_env_override_silences_advisory(monkeypatch):
    out, _, code = _run(
        f'tg --tag decision "{_BARE}"', monkeypatch,
        env={"ALLOW_RAW_DECISION_REQUEST": "1"},
    )
    assert code == 0
    assert _decision(out) == "allow"
    assert _message(out) is None


def test_inline_sentinel_silences_advisory(monkeypatch):
    out, _, _ = _run(
        f'tg --tag decision "{_BARE}"  # decision-request-ok: terse follow-up', monkeypatch
    )
    assert _message(out) is None


# ── FAIL-OPEN ───────────────────────────────────────────────────────────────────────────

def test_malformed_event_allows(monkeypatch):
    out, err, code = _run(None, monkeypatch, raw_event="not json{")
    assert code == 0
    assert _decision(out) == "allow"
    assert "fail-open" in err


def test_unparseable_command_allows(monkeypatch):
    # An unbalanced quote in a `tg --tag decision` segment → shlex raises → that segment is
    # skipped → plain allow (fail-open at the parse level, no crash).
    out, _, code = _run('tg --tag decision "unterminated', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── KNOWN BOUNDARY: a pipe/semicolon char INSIDE the quoted body ─────────────────────────
# The command is split on raw `&&`/`;`/`|` before shlex-tokenizing each segment, so a body
# that itself contains one of those chars inside quotes (`"use A | B"`) is torn mid-quote;
# the resulting segment has an unbalanced quote, shlex raises, and the segment is skipped —
# so no advisory fires. This is a false NEGATIVE only (the request still SENDS; the human
# just doesn't get the rewrite nudge), never a false positive, and matches the hook's
# advisory/fail-open posture. Documented here so the boundary is explicit, not silent. A full
# quote-aware splitter would close it but is over-engineering for an advisory nudge.

@pytest.mark.parametrize("command", [
    'tg --tag decision "should we use A | B?"',     # `|` inside the quoted body
    'tg --tag decision "do X; then Y?"',            # `;` inside the quoted body
])
def test_separator_inside_quoted_body_is_a_known_miss(command, monkeypatch):
    out, _, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
    # The send is never blocked; the only effect of the boundary is a missed advisory.
    assert _message(out) is None
