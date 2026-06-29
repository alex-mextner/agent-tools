"""Tests for tools/mcp-skill-usage analytics script.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_mcp_skill_usage.py -q

All tests are network-free and file-free (JSONL data is built in-memory).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Load the script as a module (it has no .py extension)
_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "mcp-skill-usage"
_spec = importlib.util.spec_from_loader(
    "mcp_skill_usage",
    importlib.machinery.SourceFileLoader("mcp_skill_usage", str(_SCRIPT)),
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _make_jsonl(events: list[dict]) -> str:
    """Return a JSONL string from a list of event dicts."""
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _assistant_event(tool_name: str, tool_input: dict, timestamp: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": "test-session",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": tool_name, "input": tool_input}
            ],
        },
    }


def _write_session(tmp_dir: str, filename: str, events: list[dict]) -> str:
    path = os.path.join(tmp_dir, filename)
    with open(path, "w") as f:
        f.write(_make_jsonl(events))
    return path


# ── iter_session_tools ──────────────────────────────────────────────────────


def test_iter_yields_mcp_tool():
    with tempfile.TemporaryDirectory() as td:
        path = _write_session(td, "s.jsonl", [
            _assistant_event("mcp__serena__find_symbol", {}, "2026-06-20T10:00:00Z"),
        ])
        results = list(_mod.iter_session_tools(path))
    assert len(results) == 1
    date, raw, disp = results[0]
    assert date == "2026-06-20"
    assert raw == "mcp__serena__find_symbol"
    assert disp == "mcp__serena__find_symbol"


def test_iter_yields_skill_tool():
    with tempfile.TemporaryDirectory() as td:
        path = _write_session(td, "s.jsonl", [
            _assistant_event("Skill", {"skill": "tdd-red-first"}, "2026-06-21T12:00:00Z"),
        ])
        results = list(_mod.iter_session_tools(path))
    assert len(results) == 1
    _, raw, disp = results[0]
    assert raw == "Skill"
    assert disp == "Skill:tdd-red-first"


def test_iter_non_mcp_non_skill_has_none_disp():
    """Bash/Read yield entries with disp=None (needed for look-ahead sequence)."""
    with tempfile.TemporaryDirectory() as td:
        path = _write_session(td, "s.jsonl", [
            _assistant_event("Bash", {"command": "ls"}, "2026-06-21T12:00:00Z"),
            _assistant_event("Read", {"file_path": "/tmp/x"}, "2026-06-21T12:01:00Z"),
        ])
        results = list(_mod.iter_session_tools(path))
    assert len(results) == 2
    assert all(disp is None for _, _, disp in results)


def test_iter_skips_malformed_json():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "bad.jsonl")
        with open(path, "w") as f:
            f.write('{"type":"assistant","tool_use":"blah"\n')  # malformed
        results = list(_mod.iter_session_tools(path))
    assert results == []


# ── _process_session / collect_stats ────────────────────────────────────────


def test_collect_stats_basic():
    with tempfile.TemporaryDirectory() as td:
        proj_dir = os.path.join(td, "proj-a")
        os.makedirs(proj_dir)
        _write_session(proj_dir, "s1.jsonl", [
            _assistant_event("mcp__foo__bar", {}, "2026-06-25T10:00:00Z"),
            _assistant_event("Bash", {"command": "ls"}, "2026-06-25T10:01:00Z"),
        ])
        stats = _mod.collect_stats(td, "")
    assert "mcp__foo__bar" in stats
    s = stats["mcp__foo__bar"]
    assert s["calls"] == 1
    assert s["last"] == "2026-06-25"
    assert s["hits"] == 1   # Bash follows within LOOK_AHEAD
    assert s["total"] == 1


def test_cutoff_filters_old_calls():
    with tempfile.TemporaryDirectory() as td:
        proj_dir = os.path.join(td, "proj-b")
        os.makedirs(proj_dir)
        _write_session(proj_dir, "s.jsonl", [
            _assistant_event("mcp__foo__old", {}, "2026-05-01T10:00:00Z"),
            _assistant_event("mcp__foo__new", {}, "2026-06-20T10:00:00Z"),
        ])
        stats = _mod.collect_stats(td, "2026-06-01")
    assert "mcp__foo__old" not in stats
    assert "mcp__foo__new" in stats


def test_effectiveness_no_followup():
    with tempfile.TemporaryDirectory() as td:
        proj_dir = os.path.join(td, "proj-c")
        os.makedirs(proj_dir)
        # MCP call at the very end — nothing follows it
        _write_session(proj_dir, "s.jsonl", [
            _assistant_event("mcp__foo__bar", {}, "2026-06-20T10:00:00Z"),
        ])
        stats = _mod.collect_stats(td, "")
    s = stats["mcp__foo__bar"]
    assert s["hits"] == 0
    assert s["total"] == 1


# ── format_table ────────────────────────────────────────────────────────────


def test_format_table_empty():
    out = _mod.format_table({}, 50)
    assert "No data" in out


def test_format_table_sorts_by_calls():
    stats = {
        "mcp__a__x": {"calls": 3, "sessions": {"s1"}, "last": "2026-06-20", "hits": 3, "total": 3},
        "mcp__b__y": {"calls": 10, "sessions": {"s1", "s2"}, "last": "2026-06-25", "hits": 5, "total": 10},
    }
    out = _mod.format_table(stats, 50)
    lines = out.strip().splitlines()
    # First data row (after header and sep) must be the higher-call entry
    data_rows = [l for l in lines if l.startswith("mcp__")]
    assert data_rows[0].startswith("mcp__b__y")
    assert data_rows[1].startswith("mcp__a__x")
