"""Tests for the skills-marker-writer agent-hook (pre-skill).

Covers the marker contract skills-read-gate depends on:
  WRITE     — a Skill-tool event with a plain skill name touches the marker file.
  FRESH     — writing twice bumps mtime (freshness, not just existence).
  NESTED    — a directory-scoped skill name (`apps/web:deploy`) nests under the marker dir.
  REJECT    — traversal/absolute/oversized/NUL skill names never escape the marker dir and
              never raise; the hook always still emits allow.
  NO-OP     — an event with no skill name (or a non-Skill event with no args.skill) is a
              silent allow.
  END-TO-END— skills-read-gate sees the marker this hook wrote as fresh.

Hermetic: SKILLS_INVOKED_DIR is redirected into tmp_path via env (and the module constant
re-pointed), so nothing touches the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_skills_marker_writer.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "skills-marker-writer"
    / "skills_marker_writer.py"
)
_spec = importlib.util.spec_from_file_location("skills_marker_writer", _HOOK)
assert _spec and _spec.loader
smw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smw)

_READ_GATE_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "skills-read-gate"
    / "skills_read_gate.py"
)
_rg_spec = importlib.util.spec_from_file_location("skills_read_gate", _READ_GATE_HOOK)
assert _rg_spec and _rg_spec.loader
srg = importlib.util.module_from_spec(_rg_spec)
_rg_spec.loader.exec_module(srg)


def _run(skill, monkeypatch, *, invoked: Path, extra_args: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    args: dict = dict(extra_args or {})
    if skill is not None:
        args["skill"] = skill
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo", "args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(smw, "INVOKED_DIR", invoked)
    code = smw.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_writes_marker_for_plain_skill_name(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    out, _err, code = _run("delegate-work-to-subagents", monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"
    marker = invoked / "delegate-work-to-subagents"
    assert marker.is_file()


def test_marker_mtime_bumps_on_repeat_invocation(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    _run("visual-proof-cycle", monkeypatch, invoked=invoked)
    marker = invoked / "visual-proof-cycle"
    first_mtime = marker.stat().st_mtime
    # force a real filesystem-visible tick — some filesystems have 1s mtime resolution
    stale_time = first_mtime - 10
    os.utime(marker, (stale_time, stale_time))
    assert marker.stat().st_mtime < first_mtime
    _run("visual-proof-cycle", monkeypatch, invoked=invoked)
    assert marker.stat().st_mtime > stale_time


def test_directory_scoped_skill_name_nests_under_marker_dir(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    out, _err, code = _run("apps/web:deploy", monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"
    marker = invoked / "apps" / "web:deploy"
    assert marker.is_file()


def test_traversal_and_absolute_names_never_escape_marker_dir(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    outside_marker = tmp_path / "outside-marker"
    assert not outside_marker.exists()

    for bad in ("../outside-marker", "/etc/outside-marker", "a/../../outside-marker"):
        out, err, code = _run(bad, monkeypatch, invoked=invoked)
        assert code == 0, err
        assert _decision(out) == "allow"  # never blocks, even when it refuses to write

    assert not outside_marker.exists()
    # nothing was written inside invoked either for the rejected names
    if invoked.exists():
        written = {p.name for p in invoked.rglob("*") if p.is_file()}
        assert "outside-marker" not in written


def test_oversized_and_nul_names_are_rejected_without_raising(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    too_long = "x" * 500
    out, _err, code = _run(too_long, monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"
    assert not invoked.exists() or not any(invoked.rglob("*"))

    out, _err, code = _run("bad\x00name", monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"


def test_missing_skill_name_is_a_silent_noop(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    out, _err, code = _run(None, monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"
    assert not invoked.exists()


def test_non_string_skill_name_is_a_silent_noop(tmp_path, monkeypatch):
    invoked = tmp_path / "skills-invoked"
    out, _err, code = _run(42, monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"
    assert not invoked.exists()


def test_malformed_stdin_fails_open(monkeypatch, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(smw, "INVOKED_DIR", tmp_path / "skills-invoked")
    code = smw.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"


def test_valid_non_object_json_fails_open(monkeypatch, tmp_path):
    """Valid JSON that parses to a list/int/str/bool/null survives json.load (only
    JSONDecodeError is caught) and has no `.get` — must still resolve to a silent allow,
    not an unhandled AttributeError with no protocol JSON on stdout."""
    for payload in ("[]", "[1]", "5", '"x"', "true", "null"):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(smw, "INVOKED_DIR", tmp_path / "skills-invoked")
        code = smw.main()
        assert code == 0, (payload, err.getvalue())
        assert _decision(out.getvalue()) == "allow", payload


def test_non_object_args_is_a_silent_noop(monkeypatch, tmp_path):
    """A truthy but non-dict `args` (e.g. a list) must not crash on `args.get(...)`."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo", "args": [1, 2]})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(smw, "INVOKED_DIR", tmp_path / "skills-invoked")
    code = smw.main()
    assert code == 0, err.getvalue()
    assert _decision(out.getvalue()) == "allow"


def test_symlink_inside_marker_dir_cannot_escape_it(tmp_path, monkeypatch):
    """The resolve-based containment guard in `_write_marker` is the SECOND, independent
    check — sanitization alone lets a plain-looking `evil/x` name through (no `..`, not
    absolute), so this is the one input class that actually exercises that guard: a
    directory INSIDE the marker dir that is a symlink pointing OUTSIDE it."""
    invoked = tmp_path / "skills-invoked"
    invoked.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (invoked / "evil").symlink_to(outside, target_is_directory=True)

    out, _err, code = _run("evil/x", monkeypatch, invoked=invoked)
    assert code == 0
    assert _decision(out) == "allow"  # never blocks, even when it refuses to write
    assert not (outside / "x").exists()


def test_written_marker_satisfies_skills_read_gate_freshness_check(tmp_path, monkeypatch):
    """End-to-end: the marker this hook writes is what skills-read-gate's own freshness
    check reads — the two hooks must agree on the marker dir/filename shape."""
    invoked = tmp_path / "skills-invoked"
    monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
    monkeypatch.setattr(srg, "FRESH_WINDOW_S", 7200)
    monkeypatch.setenv("MANDATORY_SKILLS", "delegate-work-to-subagents,visual-proof-cycle")

    # before invocation: the gate sees it as missing
    assert srg._missing_skills(subagent=False) == [
        "delegate-work-to-subagents",
        "visual-proof-cycle",
    ]

    _run("delegate-work-to-subagents", monkeypatch, invoked=invoked)
    _run("visual-proof-cycle", monkeypatch, invoked=invoked)

    # after invocation: the marker-writer's output satisfies the gate's own freshness check
    assert srg._missing_skills(subagent=False) == []
