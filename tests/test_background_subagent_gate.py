"""Tests for the background-subagent-gate agent-hook (pre-agent).

Covers the doctrine's four cases: BLOCK (non-trivial foreground dispatch), ALLOW
(run_in_background true / trivial one-liner), SUBAGENT-EXEMPT (agent_id present), and the
ESCAPE hatch (env + reason allows; reasonless override still blocks).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_background_subagent_gate.py -q
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
    / "background-subagent-gate"
    / "background_subagent_gate.py"
)
_spec = importlib.util.spec_from_file_location("background_subagent_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_LONG = "x" * 300  # a clearly non-trivial single-line prompt (> 200 chars)


def _run(event, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear escape-hatch env so a stray ambient value can't leak into a test.
    for k in ("ALLOW_FOREGROUND_SUBAGENT", "ALLOW_FOREGROUND_SUBAGENT_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_block_non_trivial_foreground_dispatch(monkeypatch):
    out, _err, code = _run({"args": {"prompt": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "BACKGROUND" in payload["message"]


def test_allow_background_dispatch(monkeypatch):
    out, _err, code = _run({"args": {"run_in_background": True, "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_background_dispatch_string_true(monkeypatch):
    out, _err, code = _run({"args": {"run_in_background": "true", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_trivial_one_liner(monkeypatch):
    out, _err, code = _run({"args": {"prompt": "rename foo to bar in one file"}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_subagent_exempt_allows_even_foreground(monkeypatch):
    out, _err, code = _run({"args": {"agent_id": "sub-1", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_escape_hatch_with_reason_allows(monkeypatch):
    out, _err, code = _run(
        {"args": {"prompt": _LONG}},
        monkeypatch,
        {"ALLOW_FOREGROUND_SUBAGENT": "1", "ALLOW_FOREGROUND_SUBAGENT_REASON": "latency probe"},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_reasonless_override_still_blocks(monkeypatch):
    out, _err, code = _run(
        {"args": {"prompt": _LONG}},
        monkeypatch,
        {"ALLOW_FOREGROUND_SUBAGENT": "1"},  # no reason
    )
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── #6: triviality is judged on the LONGEST of prompt/description, not the first non-empty ─

def test_short_prompt_long_description_is_not_trivial(monkeypatch):
    """A short `prompt` paired with a long `description` must NOT be judged trivial — the gate
    must block the foreground dispatch. Judging only the first non-empty value let this slip
    through (#6)."""
    out, _err, code = _run({"args": {"prompt": "x", "description": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_long_prompt_short_description_is_not_trivial(monkeypatch):
    """Symmetric: a long prompt with a short description is also non-trivial → block."""
    out, _err, code = _run({"args": {"prompt": _LONG, "description": "x"}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_short_prompt_and_short_description_is_trivial(monkeypatch):
    """When BOTH are short and single-line the dispatch is trivial → allow inline."""
    out, _err, code = _run({"args": {"prompt": "rename foo", "description": "small refactor"}},
                           monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_multiline_description_is_not_trivial(monkeypatch):
    """A multi-line description (even if short per line) is non-trivial → block (#6)."""
    out, _err, code = _run({"args": {"prompt": "x", "description": "step 1\nstep 2"}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── #11: description-only (no prompt) is judged on its own length ─────────────────────────

def test_description_only_long_is_not_trivial(monkeypatch):
    """A dispatch carrying only a long `description` (no prompt) must block (#11)."""
    out, _err, code = _run({"args": {"description": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_description_only_short_is_trivial(monkeypatch):
    """A short description-only dispatch is trivial → allow (#11)."""
    out, _err, code = _run({"args": {"description": "tidy imports in one file"}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
