"""Tests for the chat-dictionary-gate agent-hook (stop, fail-OPEN banned-word gate).

Blocks the agent's Stop when its own just-written reply (extracted from the session
transcript) contains a banned word from ~/.claude/DICT.json — the same dictionary already
enforced for outgoing Telegram messages via `tg`, applied now to the Claude Code chat
surface itself, which had zero enforcement before this hook (agent-tools#548).

Key divergences from its stop-completion-selfcheck sibling, pinned here:
  - Cyrillic scoping: a rule with `only_if_cyrillic` applies only when the WHOLE current
    turn's assistant text contains a Cyrillic codepoint — judged once, not per-rule.
  - Fail-OPEN on a broken dictionary (tg fails closed; this hook never traps the user's
    turn over a config typo).
  - A loop-guard CAP (not a cooldown): every Stop with a real violation blocks, but a
    per-session consecutive-violation counter caps retries so a genuinely stuck rewrite
    loop can't wedge the session forever.
  - Retry-boundary scanning: a real Stop block injects a synthetic `type:"user"`,
    `isMeta:true` record whose string content starts with "Stop hook feedback:" (verified
    empirically against real transcripts, see the hook's own module docstring). That
    record is NOT a genuine user turn boundary (isMeta), but it MUST still stop the
    reverse scan the same way a real user message does — otherwise a corrected rewrite
    forever inherits the stale violating text from the attempt the block was reacting to,
    and the gate could never let a fixed turn through.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_chat_dictionary_gate.py -q
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
    / "chat-dictionary-gate"
    / "chat_dict_gate.py"
)
_spec = importlib.util.spec_from_file_location("chat_dict_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
# Register before exec: the hook module uses `from __future__ import annotations` +
# `@dataclass`, and dataclass's ClassVar/InitVar string-annotation resolution looks the
# module up via `sys.modules[cls.__module__]` while its own body is still executing — a
# module loaded via spec_from_file_location isn't in sys.modules unless we put it there
# first, so without this line dataclass processing crashes with "NoneType has no
# attribute '__dict__'" the moment the hook module defines its first @dataclass.
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


def _run(event: dict, monkeypatch) -> tuple[dict, int, str]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = gate.main()
    return json.loads(out.getvalue()), code, err.getvalue()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    marker_dir = tmp_path / "chat-dict-gate"
    monkeypatch.setattr(gate, "MARKER_DIR", marker_dir)
    monkeypatch.setattr(gate, "FIRINGS_LOG", marker_dir / "firings.jsonl")
    # Point at a real, valid dictionary by default so tests don't accidentally exercise
    # the "disabled" path; individual tests override this to a custom file/dir as needed.
    monkeypatch.setattr(gate, "DICT_PATH_OVERRIDE", "")


def _write_dict(tmp_path, rules: list[dict], *, filename: str = "DICT.json") -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")
    return path


def _default_dict_path(monkeypatch, tmp_path, rules: list[dict]) -> Path:
    """Point the hook's DEFAULT dictionary path at a throwaway file."""
    path = _write_dict(tmp_path, rules)
    monkeypatch.setattr(gate, "DEFAULT_DICT_PATH", str(path))
    return path


POCHTIT_RULE = {
    "id": "pochtit-as-fix",
    "pattern": r"\bпочт(?:ил[аио]?|или|ить)\b",
    "flags": "iu",
    "replacement": "починил / починен / патчил",
    "why": "There is no verb 'почтить' meaning 'to fix'.",
}

FORK_RULE = {
    "id": "fork-to-alex",
    "pattern": r"\bфорк[а-яё]*|\bfork(?:s|ed|ing)?\b",
    "flags": "iu",
    "only_if_cyrillic": True,
    "replacement": "субагент / субагенты",
    "why": "Dispatched subagents are an implementation detail; never say 'fork' to Alex.",
}

LAND_RULE = {
    "id": "land-landing-in-russian",
    "pattern": r"\bland(?:ing|ed|s)?\b",
    "flags": "iu",
    "only_if_cyrillic": True,
    "replacement": "применяется / применён / влит",
    "why": "English 'land/landing' must not appear in Russian text.",
}


def _write_transcript(tmp_path, records: list[dict], *, name: str = "transcript.jsonl") -> str:
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return str(path)


def _user_msg(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_tool_use_only(name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def _stop_hook_feedback(prompt: str = "Before finishing, run the completion self-check:\n...") -> dict:
    """The real record shape a Stop-hook block injects (verified against live transcripts,
    e.g. ~/.claude/projects/-Users-ultra-xp-rig-cli/5a9d5cb6-*.jsonl line 40): type "user",
    isMeta true, message.content a STRING (not a content-block list) prefixed exactly this
    way."""
    return {
        "type": "user",
        "isMeta": True,
        "message": {"role": "user", "content": f"Stop hook feedback:\n{prompt}"},
    }


# ---------------------------------------------------------------------------
# Clean turn -> allow
# ---------------------------------------------------------------------------


def test_clean_turn_allows_and_resets_counter(monkeypatch, tmp_path):
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("Done, all good.")])
    event = {"event_id": "sess-clean", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    sid = gate.session_id(event)
    assert gate._read_counter(sid) == 0


# ---------------------------------------------------------------------------
# Banned word -> block, with full message contract
# ---------------------------------------------------------------------------


def test_pochtit_violation_blocks_with_full_message_contract(monkeypatch, tmp_path):
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path, [_user_msg("что там с багом?"), _assistant_text("Баг я почтил вчера.")]
    )
    event = {"event_id": "sess-pochtit", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert out["decision"] == "block"
    msg = out["message"]
    assert "почтил" in msg
    assert "pochtit-as-fix" in msg
    assert "There is no verb 'почтить' meaning 'to fix'." in msg
    assert "починил / починен / патчил" in msg


# ---------------------------------------------------------------------------
# Cyrillic scoping
# ---------------------------------------------------------------------------


def test_english_fork_in_ordinary_sentence_is_allowed(monkeypatch, tmp_path):
    """only_if_cyrillic must protect plain English text (e.g. a subagent's own dispatch
    report) from a rule that exists only to police Russian-language chat with Alex."""
    _default_dict_path(monkeypatch, tmp_path, [FORK_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("what did the dispatched agent do?"),
            _assistant_text("The subagent forked off three background checks and reported back."),
        ],
    )
    event = {"event_id": "sess-fork-en", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0


def test_inlined_sidechain_english_report_does_not_flip_cyrillic_scoping(monkeypatch, tmp_path):
    """Regression (review-cli finding): a subagent's own record inlined into the parent
    transcript (`isSidechain: true`) must be excluded from the scan entirely — not just
    from user-boundary detection. Otherwise its plain-English "forked" text gets
    concatenated with the parent's Cyrillic reply, `has_cyrillic` becomes true, and
    `fork-to-alex` fires on text that was never part of the reply Alex actually reads."""
    _default_dict_path(monkeypatch, tmp_path, [FORK_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что сделал субагент?"),
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The subagent forked three checks."}],
                },
            },
            _assistant_text("Готово, всё проверено."),
        ],
    )
    event = {"event_id": "sess-sidechain-fork", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow", "an inlined sidechain record must not leak into the parent turn's scan"


def test_russian_landing_loanword_blocks(monkeypatch, tmp_path):
    """only_if_cyrillic must turn ON for a Russian message that borrows an English word."""
    _default_dict_path(monkeypatch, tmp_path, [LAND_RULE])
    transcript = _write_transcript(
        tmp_path,
        [_user_msg("что с пулл реквестом?"), _assistant_text("PR landing уже в проде.")],
    )
    event = {"event_id": "sess-landing-ru", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert "land-landing-in-russian" in out["message"]
    assert "landing" in out["message"]


# ---------------------------------------------------------------------------
# Loop guard: consecutive violations cap, distinct from a cooldown
# ---------------------------------------------------------------------------


def test_loop_guard_caps_consecutive_blocks_then_allows(monkeypatch, tmp_path):
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    monkeypatch.setattr(gate, "LOOP_GUARD_CAP", 3)
    event_id = "sess-loop"

    def _violating_event(n: int) -> dict:
        transcript = _write_transcript(
            tmp_path,
            [_user_msg("fix it"), _assistant_text(f"почтил попытка {n}")],
            name=f"t{n}.jsonl",
        )
        return {"event_id": event_id, "args": {"transcript_path": transcript}}

    for n in range(1, 4):  # 3 consecutive violations, at/under the cap -> block every time
        out, code, err = _run(_violating_event(n), monkeypatch)
        assert out["decision"] == "block", f"attempt {n} should still block"
        assert code == gate.BLOCK_EXIT_CODE

    # 4th consecutive violation exceeds the cap -> give up rather than wedge the session.
    # On the WIRE this collapses to a plain "allow" (agents-hooks/v1 only needs to know
    # the stop was allowed, not the internal bookkeeping reason — a review finding); the
    # distinct "allow_loop_guard_cap" label is preserved only in the firings log.
    out, code, err = _run(_violating_event(4), monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert "loop" in err.lower() or "cap" in err.lower()
    firings = [json.loads(line) for line in gate.FIRINGS_LOG.read_text(encoding="utf-8").splitlines()]
    assert firings[-1]["decision"] == "allow_loop_guard_cap"

    # A clean turn resets the streak counter to 0.
    clean_transcript = _write_transcript(tmp_path, [_user_msg("ok"), _assistant_text("All good.")], name="clean.jsonl")
    out, code, err = _run({"event_id": event_id, "args": {"transcript_path": clean_transcript}}, monkeypatch)
    assert out["decision"] == "allow"
    sid = gate.session_id({"event_id": event_id})
    assert gate._read_counter(sid) == 0

    # A fresh violation right after the reset blocks again FROM 1 -> the cap is
    # per-streak, not a lifetime count (review-cli finding: this exact check was
    # previously described in the README but not actually pinned by any test).
    out, code, err = _run(_violating_event(5), monkeypatch)
    assert out["decision"] == "block"
    assert code == gate.BLOCK_EXIT_CODE
    assert gate._read_counter(sid) == 1


def test_loop_guard_and_retry_boundary_together_on_one_growing_transcript(monkeypatch, tmp_path):
    """Regression (review-cli finding): the test above proves the cap arithmetic using a
    FRESH 2-record transcript per attempt, which only proves the cap under conditions
    where each Stop trivially sees exactly one violation. Production has ONE transcript
    that keeps growing across retries: [user, A1, feedback, A2, feedback, A3, feedback,
    A4, ...] — the cap's correctness actually depends on `_is_retry_boundary` isolating
    exactly one attempt per Stop from that single, ever-growing file. This test drives
    that real shape directly and asserts the counter after each step, not just the final
    decision."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    monkeypatch.setattr(gate, "LOOP_GUARD_CAP", 3)
    transcript_path = tmp_path / "growing.jsonl"
    event = {"event_id": "sess-growing", "args": {"transcript_path": str(transcript_path)}}
    sid = gate.session_id(event)

    def _append(*records: dict) -> None:
        with open(transcript_path, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    _append(_user_msg("fix it"))
    for n, expected_counter in ((1, 1), (2, 2), (3, 3)):
        _append(_assistant_text(f"почтил попытка {n}"))
        out, code, err = _run(event, monkeypatch)
        assert out["decision"] == "block", f"attempt {n} should still block"
        assert gate._read_counter(sid) == expected_counter
        _append(_stop_hook_feedback())

    _append(_assistant_text("почтил попытка 4"))
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow", "the 4th consecutive violation on the SAME growing transcript must exceed the cap"
    assert gate._read_counter(sid) == 0


# ---------------------------------------------------------------------------
# Retry-boundary scanning: a rewrite after a real Stop block must be judged on its OWN
# text, not stuck re-reading the pre-block violating text forever.
# ---------------------------------------------------------------------------


def test_rewrite_after_a_stop_block_that_fixed_the_wording_is_allowed(monkeypatch, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            _assistant_text("Баг я почтил вчера."),
            _stop_hook_feedback(),
            _assistant_text("Баг я починил вчера."),
        ],
    )
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    event = {"event_id": "sess-retry-fixed", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow", (
        "the retry-boundary must stop the scan at the Stop-hook-feedback record so the "
        "already-superseded violating text from before the block is not re-scanned forever"
    )
    assert code == 0
    sid = gate.session_id(event)
    assert gate._read_counter(sid) == 0


def test_rewrite_after_a_stop_block_that_still_violates_blocks_again(monkeypatch, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            _assistant_text("Всё чисто, ничего не трогал."),
            _stop_hook_feedback(),
            _assistant_text("Баг я почтил вчера."),
        ],
    )
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    event = {"event_id": "sess-retry-still-bad", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block"
    assert code == gate.BLOCK_EXIT_CODE
    assert "почтил" in out["message"]


# ---------------------------------------------------------------------------
# Dictionary lifecycle: fail-open divergence from tg-cli
# ---------------------------------------------------------------------------


def test_missing_default_dictionary_allows_silently(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "DEFAULT_DICT_PATH", str(tmp_path / "does-not-exist.json"))
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-no-default-dict", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert err == "", "a missing DEFAULT dictionary is not an error — no stderr noise"
    assert not gate.MARKER_DIR.exists(), (
        "a machine with no dictionary at all must accumulate NO state (review finding): "
        "a marker/firings write on every Stop would be unbounded, pointless disk growth "
        "on every machine that isn't even using this feature"
    )


def test_missing_explicit_dictionary_override_allows_with_warning(monkeypatch, tmp_path):
    """Deliberate divergence from tg (which fails CLOSED on an explicit-but-missing path):
    this hook's whole contract is on_error=open, so even an explicit misconfiguration must
    not trap the user's turn — but it DOES warn, unlike the silent-default case above."""
    monkeypatch.setattr(gate, "DICT_PATH_OVERRIDE", str(tmp_path / "nope.json"))
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-explicit-missing", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert err != "", "an explicit override pointing nowhere is a misconfiguration worth a warning"


def test_malformed_json_dictionary_allows_with_warning(monkeypatch, tmp_path):
    path = tmp_path / "DICT.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(gate, "DEFAULT_DICT_PATH", str(path))
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-malformed-json", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert "DICT.json" in err or str(path) in err
    assert "not valid JSON" in err or "JSON" in err


def test_rule_with_uncompilable_regex_allows_with_warning(monkeypatch, tmp_path):
    bad_rule = {
        "id": "broken-rule",
        "pattern": "(unclosed",
        "flags": "iu",
        "replacement": "n/a",
        "why": "n/a",
    }
    _default_dict_path(monkeypatch, tmp_path, [bad_rule])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-bad-regex", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert "broken-rule" in err


def test_bad_rule_does_not_disable_the_other_valid_rules(monkeypatch, tmp_path):
    bad_rule = {
        "id": "broken-rule",
        "pattern": "(unclosed",
        "flags": "iu",
        "replacement": "n/a",
        "why": "n/a",
    }
    _default_dict_path(monkeypatch, tmp_path, [bad_rule, POCHTIT_RULE])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-partial-dict", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block", "one broken rule must not take down the whole dictionary"
    assert "pochtit-as-fix" in out["message"]
    assert "broken-rule" in err


def test_non_string_flags_does_not_crash_the_whole_dictionary(monkeypatch, tmp_path):
    """Regression (review-cli finding): a non-string, non-iterable `flags` (e.g. `5`)
    used to raise an UNCAUGHT TypeError from `_map_flags`'s `for ch in flags or ""` —
    escaping `_compile_rule`'s narrower `except (re.error, ValueError)` and propagating
    all the way out of `main()`, taking down every rule including ones that had already
    compiled fine. Must instead be treated exactly like any other invalid rule shape:
    that ONE rule dropped, warned about, everything else still enforced."""
    bad_flags_rule = dict(POCHTIT_RULE, id="bad-flags-rule", flags=5)
    _default_dict_path(monkeypatch, tmp_path, [bad_flags_rule, LAND_RULE])
    transcript = _write_transcript(
        tmp_path, [_user_msg("что с пулл реквестом?"), _assistant_text("PR landing уже в проде.")]
    )
    event = {"event_id": "sess-nonstring-flags", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block", "a non-string flags value must not crash main() or drop other rules"
    assert "land-landing-in-russian" in out["message"]
    assert "bad-flags-rule" in err


def test_transient_dict_failure_between_violations_still_resets_the_streak(monkeypatch, tmp_path):
    """Regression (review-cli finding): if a session's dictionary transiently breaks (or
    is briefly missing) on a Stop sitting BETWEEN two real violations, that Stop must still
    reset any EXISTING counter to 0 — otherwise a later real violation would resume from a
    stale prior count and trip the loop-guard cap earlier than genuinely-consecutive
    violations warrant."""
    dict_path = _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    monkeypatch.setattr(gate, "LOOP_GUARD_CAP", 3)
    event_id = "sess-transient-dict-failure"
    sid = gate.session_id({"event_id": event_id})

    transcript_1 = _write_transcript(tmp_path, [_user_msg("fix it"), _assistant_text("почтил раз")], name="t1.jsonl")
    out, code, err = _run({"event_id": event_id, "args": {"transcript_path": transcript_1}}, monkeypatch)
    assert out["decision"] == "block"
    assert gate._read_counter(sid) == 1

    # The dictionary transiently breaks on the NEXT Stop (no violation is even evaluated).
    dict_path.write_text("{not valid json", encoding="utf-8")
    transcript_2 = _write_transcript(tmp_path, [_user_msg("ok"), _assistant_text("checking...")], name="t2.jsonl")
    out, code, err = _run({"event_id": event_id, "args": {"transcript_path": transcript_2}}, monkeypatch)
    assert out["decision"] == "allow"
    assert gate._read_counter(sid) == 0, "an existing streak must be reset even on the not-rules early-return path"

    # The dictionary is fixed; a fresh real violation must count from 1, not resume at 2.
    dict_path.write_text(json.dumps({"version": 1, "rules": [POCHTIT_RULE]}), encoding="utf-8")
    transcript_3 = _write_transcript(tmp_path, [_user_msg("fix it"), _assistant_text("почтил два")], name="t3.jsonl")
    out, code, err = _run({"event_id": event_id, "args": {"transcript_path": transcript_3}}, monkeypatch)
    assert out["decision"] == "block"
    assert gate._read_counter(sid) == 1


def test_rule_without_why_still_compiles_and_enforces(monkeypatch, tmp_path):
    """Parity with tg-cli (review-cli finding): tg-cli's own `compileRule` does not
    require "why" — only id/pattern/replacement. A valid rule missing "why" must still be
    enforced here, with a fallback string in the block message, not silently dropped."""
    rule_without_why = {k: v for k, v in POCHTIT_RULE.items() if k != "why"}
    _default_dict_path(monkeypatch, tmp_path, [rule_without_why])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-no-why", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block"
    assert "pochtit-as-fix" in out["message"]
    assert err == "", "a missing (optional) why must not itself warn"


def test_dictionary_with_leading_bom_still_parses(monkeypatch, tmp_path):
    """Parity with tg-cli (review-cli finding): tg-cli strips a leading UTF-8 BOM before
    parsing; a hand-edited file saved with one must not silently disable this hook while
    Telegram keeps enforcing it fine."""
    path = tmp_path / "DICT.json"
    path.write_text("﻿" + json.dumps({"version": 1, "rules": [POCHTIT_RULE]}), encoding="utf-8")
    monkeypatch.setattr(gate, "DEFAULT_DICT_PATH", str(path))
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-bom", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block"
    assert err == ""


def test_unsupported_dictionary_version_allows_with_warning(monkeypatch, tmp_path):
    """Parity with tg-cli (review-cli finding): tg-cli refuses an unsupported `version`
    rather than guessing at possibly-changed rule semantics. This hook mirrors the refusal
    as a fail-open warning (not a hard refusal, per its own on_error=open posture)."""
    path = tmp_path / "DICT.json"
    path.write_text(json.dumps({"version": 2, "rules": [POCHTIT_RULE]}), encoding="utf-8")
    monkeypatch.setattr(gate, "DEFAULT_DICT_PATH", str(path))
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-bad-version", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert "version" in err.lower()


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_env_always_allows_with_no_writes(monkeypatch, tmp_path):
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    monkeypatch.setenv("CHAT_DICT_GATE_DISABLE", "1")
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-killswitch", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert not gate.MARKER_DIR.exists(), "kill switch must skip all marker/log writes"


# ---------------------------------------------------------------------------
# Regression tests for review findings (review-cli, agent-tools#548)
# ---------------------------------------------------------------------------


def test_multiple_text_blocks_in_one_record_keep_their_original_order(monkeypatch, tmp_path):
    """Regression: `_extract_current_turn_text` used to reverse the FLAT list of text
    blocks across all records, which silently reversed the WITHIN-record block order too
    (a record with blocks [A1, A2] came out as A2-then-A1). Two records each with two text
    blocks makes the bug concrete and checkable via order-sensitive matching."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там?"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "первая часть,"},
                        {"type": "text", "text": "а баг я почтил вчера"},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "и ещё кое-что"},
                        {"type": "text", "text": "готово."},
                    ],
                },
            },
        ],
    )
    text = gate._extract_current_turn_text(transcript)
    assert text == "первая часть,\nа баг я почтил вчера\nи ещё кое-что\nготово."


def test_unwritable_marker_dir_fails_open_instead_of_wedging_the_session(monkeypatch, tmp_path):
    """Regression (review-cli finding, agent-tools#548): a read-only/full MARKER_DIR must
    not silently turn this hook's fail-OPEN contract into fail-CLOSED-forever. If the
    counter for a real "block" decision can't be persisted, a future Stop would always
    read the counter as 0 (the violation was never recorded) and the loop-guard cap could
    never trip — an un-cappable, permanent block loop. `_write_counter` failing on a real
    violation must escalate straight to an allow instead."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    # A regular FILE where MARKER_DIR is expected to be a directory makes `mkdir` (called
    # inside `_write_counter`) fail with OSError/FileExistsError on every attempt,
    # reliably simulating "the marker dir cannot be written to" without touching real
    # filesystem permissions (which behave inconsistently across CI/sandboxes/root).
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gate, "MARKER_DIR", blocked_path)
    monkeypatch.setattr(gate, "FIRINGS_LOG", tmp_path / "firings.jsonl")  # still writable

    transcript = _write_transcript(tmp_path, [_user_msg("fix it"), _assistant_text("почтил")])
    event = {"event_id": "sess-unwritable-marker", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow", "a violation must not wedge the turn when its own counter can't be persisted"
    assert code == 0
    assert "persist" in err.lower() or "counter" in err.lower()
    firings = [json.loads(line) for line in gate.FIRINGS_LOG.read_text(encoding="utf-8").splitlines()]
    assert firings[-1]["decision"] == "allow_counter_write_failed", (
        "a persistence failure must log a DIFFERENT decision than a genuine loop-guard-cap "
        "exceed — they need different remediation and must be distinguishable in firings.jsonl"
    )


def test_malformed_stdin_event_allows(monkeypatch):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json"))
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", err_buf)
    code = gate.main()
    result = json.loads(out_buf.getvalue())
    assert result["decision"] == "allow"
    assert code == 0


def test_unreadable_transcript_path_returns_empty_text_and_allows(monkeypatch, tmp_path):
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    event = {"event_id": "sess-unreadable-transcript", "args": {"transcript_path": str(tmp_path / "nope.jsonl")}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert code == 0
    assert gate._extract_current_turn_text(str(tmp_path / "nope.jsonl")) == ""


def test_unknown_regex_flag_character_drops_rule_with_warning(monkeypatch, tmp_path):
    bad_flag_rule = dict(POCHTIT_RULE, flags="ix")  # "x" is not a flag this hook maps
    _default_dict_path(monkeypatch, tmp_path, [bad_flag_rule])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": "sess-unknown-flag", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    assert "unknown flag" in err.lower() or "pochtit-as-fix" in err


@pytest.mark.parametrize("flag_char", ["g", "m", "s"])
def test_js_flags_g_m_s_are_mapped_not_rejected(monkeypatch, tmp_path, flag_char):
    """`g`/`m`/`s` are valid JS regex flags DICT.json's own schema allows (today's real
    file only uses "iu", but a future edit adding e.g. "gi" must not silently drop the
    whole rule — a review finding)."""
    rule = dict(POCHTIT_RULE, flags=f"i{flag_char}")
    _default_dict_path(monkeypatch, tmp_path, [rule])
    transcript = _write_transcript(tmp_path, [_user_msg("hi"), _assistant_text("почтил")])
    event = {"event_id": f"sess-flag-{flag_char}", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block", f"flag {flag_char!r} must not cause the rule to be dropped"
    assert err == "", f"flag {flag_char!r} is a mapped no-op/real flag, not an unknown one"


def test_realistic_retry_boundary_with_intermediate_attachment_and_system_records(monkeypatch, tmp_path):
    """The README's own forensic evidence (a real transcript) shows an `attachment`
    (`hook_blocking_error`) record and a `system` (`stop_hook_summary`) record sitting
    BETWEEN the injected feedback record and the model's retry — not just the feedback
    record immediately followed by the retry, as the simpler tests assume."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            _assistant_text("Баг я почтил вчера."),
            _stop_hook_feedback(),
            {"type": "attachment", "attachment": {"type": "hook_blocking_error"}},
            {"type": "system", "subtype": "stop_hook_summary"},
            _assistant_text("Баг я починил вчера."),
        ],
    )
    event = {"event_id": "sess-realistic-retry", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow", "attachment/system records between the boundary and the retry must not derail the scan"


@pytest.mark.parametrize("corrupt_payload", ["[]", "null", "5", '"x"'])
def test_read_counter_treats_any_non_dict_payload_as_zero(monkeypatch, tmp_path, corrupt_payload):
    """Regression (review-cli finding): a top-level JSON scalar/list/null has no `.get`,
    which is an AttributeError, not the TypeError the old except tuple documented catching
    for a non-dict payload. Must resolve to 0 (treat as a fresh streak), not crash."""
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(gate, "MARKER_DIR", marker_dir)
    sid = "some-session"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{sid}.json").write_text(corrupt_payload, encoding="utf-8")
    assert gate._read_counter(sid) == 0


@pytest.mark.parametrize(
    "payload",
    [
        '{"consecutive_blocks": -1000000}',  # a negative value would count DOWN toward
        # the cap on every future violation, taking ~a million extra blocks to ever trip
        # the guard — a review finding.
        '{"consecutive_blocks": 1e999}',  # parses to float("inf"); int(inf) raises
        # OverflowError, which used to be uncaught — a review finding.
        '{"consecutive_blocks": NaN}',
    ],
)
def test_read_counter_treats_negative_and_non_finite_values_as_zero(monkeypatch, tmp_path, payload):
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(gate, "MARKER_DIR", marker_dir)
    sid = "some-session"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{sid}.json").write_text(payload, encoding="utf-8")
    assert gate._read_counter(sid) == 0


def test_retry_boundary_using_the_hooks_own_quoted_block_message_then_clean_rewrite(monkeypatch, tmp_path):
    """The realistic case this hook's OWN block produces: the injected feedback record's
    text contains the banned word ITSELF (quoted verbatim in the block message per the
    contract). The retry-boundary must still exclude it from the next scan even though the
    banned word appears again, right before the boundary."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    own_block_message = gate._format_block_message(
        [gate.Hit(rule_id="pochtit-as-fix", matched="почтил", replacement="починил", why="n/a")]
    )
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            _assistant_text("Баг я почтил вчера."),
            _stop_hook_feedback(own_block_message),
            _assistant_text("Баг я починил вчера."),
        ],
    )
    event = {"event_id": "sess-own-message-retry", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"


# ---------------------------------------------------------------------------
# Additional coverage (review-cli, round 4: non-blocking missing-test observations)
# ---------------------------------------------------------------------------


def test_real_user_message_with_plain_string_content_is_a_boundary(monkeypatch, tmp_path):
    """`_user_msg` (the helper every other test uses) always emits a content-block LIST;
    a real CC user record can carry plain STRING content instead — the
    `isinstance(content, str)` branch of `_is_real_user_turn_boundary` was otherwise never
    exercised by this suite."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "first, unrelated question"}},
            _assistant_text("An unrelated answer."),
            {"type": "user", "message": {"role": "user", "content": "now, plain string content again"}},
            _assistant_text("All good."),
        ],
    )
    event = {"event_id": "sess-string-user-boundary", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "allow"
    # Only the text AFTER the second (string-content) user message should have been
    # scanned — confirm indirectly via the extraction helper directly.
    assert gate._extract_current_turn_text(transcript) == "All good."


def test_whole_turn_cyrillic_scoping_across_multiple_assistant_records_in_one_turn(monkeypatch, tmp_path):
    """The core scoping promise, pinned end to end: an English record using the word
    "fork" PLUS a Russian-language record in the SAME turn must block — has_cyrillic is
    judged over the WHOLE joined turn text, not per-record."""
    _default_dict_path(monkeypatch, tmp_path, [FORK_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("what happened?"),
            _assistant_text("The subagent forked off a background check."),
            _assistant_text("Всё готово, дальше ничего не трогал."),  # no fork-related word here
        ],
    )
    event = {"event_id": "sess-whole-turn-cyrillic", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block", (
        "Cyrillic anywhere in the joined turn text must turn on only_if_cyrillic rules "
        "for the WHOLE turn, including an earlier English-only record"
    )


def test_retry_with_only_a_tool_use_block_extracts_as_empty_and_allows(monkeypatch, tmp_path):
    """Known, documented limitation (README "Known limitations"): a retry consisting only
    of a tool_use block (no text at all) extracts as "" and allows, even though the
    pre-block violating text remains the last rendered assistant text. Pinned here so a
    future change to this behavior is a deliberate, visible decision, not an accident."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            _assistant_text("Баг я почтил вчера."),
            _stop_hook_feedback(),
            _assistant_tool_use_only("Bash"),
        ],
    )
    event = {"event_id": "sess-retry-tool-use-only", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert gate._extract_current_turn_text(transcript) == ""
    assert out["decision"] == "allow"


def test_isMeta_user_record_without_the_feedback_prefix_is_not_a_boundary(monkeypatch, tmp_path):
    """A `type:"user"`, `isMeta:true` record that is NOT the Stop-hook-feedback shape
    (no "Stop hook feedback:" prefix) — e.g. a slash-command wrapper or an injected
    system-reminder-style note — must be treated as turn-continuation, like
    stop-completion-selfcheck's own `_is_real_user_turn_boundary` already does, not
    mistaken for a retry boundary. The scan must keep going past it."""
    _default_dict_path(monkeypatch, tmp_path, [POCHTIT_RULE])
    transcript = _write_transcript(
        tmp_path,
        [
            _user_msg("что там с багом?"),
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "<system-reminder>some injected note</system-reminder>"},
            },
            _assistant_text("Баг я почтил вчера."),
        ],
    )
    event = {"event_id": "sess-non-retry-ismeta", "args": {"transcript_path": transcript}}
    out, code, err = _run(event, monkeypatch)
    assert out["decision"] == "block", "a non-feedback isMeta record must not be mistaken for a retry boundary"


# ---------------------------------------------------------------------------
# Parity: every rule in the REAL ~/.claude/DICT.json compiles under this hook's Python
# re-flag mapping. Skipped when the file doesn't exist (e.g. CI, a fresh clone).
# ---------------------------------------------------------------------------


_REAL_DICT = Path("~/.claude/DICT.json").expanduser()


@pytest.mark.skipif(not _REAL_DICT.exists(), reason="no personal ~/.claude/DICT.json on this machine")
def test_real_dictionary_compiles_with_no_errors():
    rules, warning = gate._load_dictionary_from_path(_REAL_DICT, explicit=True)
    assert warning is None, f"real DICT.json produced a compile warning: {warning}"
    raw = json.loads(_REAL_DICT.read_text(encoding="utf-8"))
    assert len(rules) == len(raw["rules"]), "every real rule must compile, none silently dropped"
