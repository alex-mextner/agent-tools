"""Tests for the format-on-write agent-hook.

Hermetic: no network, no real formatters. The "does it run a formatter" cases install a
tiny fake executable into a temp repo's node_modules/.bin (so the local-bin preference is
exercised) or monkeypatch the detection table; everything resolves to `allow`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_format_on_write.py -q
    # or, if pytest is installed:  python -m pytest tests/test_format_on_write.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
from pathlib import Path

import pytest

# Import the hook module by path (it lives outside any package, next to its descriptor).
_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "format-on-write"
    / "format_on_write.py"
)
_spec = importlib.util.spec_from_file_location("format_on_write", _HOOK)
assert _spec and _spec.loader
fow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fow)


def _run(event, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Drive main() with `event` on stdin; return (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = fow.main()
    return out.getvalue(), err.getvalue(), code


def _assert_allow(out: str, code: int) -> None:
    assert code == 0
    payload = json.loads(out)
    assert payload == {"hook_api": fow.HOOK_API, "decision": "allow"}
    assert out.endswith("\n"), "protocol line must be newline-terminated"


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _install_fake_local_bin(root: Path, tool: str, body: str = "exit 0\n") -> Path:
    binp = root / "node_modules" / ".bin" / tool
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("#!/bin/sh\n" + body)
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binp


# ── escape hatch ─────────────────────────────────────────────────────────────────────────


def test_escape_hatch_skips_before_touching_anything(tmp_path, monkeypatch):
    f = tmp_path / "a.go"
    f.write_text("package main\nfunc  main(){}\n")
    before = f.read_text()
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch, {"NO_FORMAT_HOOK": "1"})
    _assert_allow(out, code)
    assert "NO_FORMAT_HOOK=1" in err
    assert f.read_text() == before  # untouched


# ── always-allow failure modes ─────────────────────────────────────────────────────────────


def test_non_dict_event_allows(tmp_path, monkeypatch):
    out, err, code = _run([1, 2, 3], monkeypatch)  # type: ignore[arg-type]
    _assert_allow(out, code)
    assert "not a JSON object" in err


def test_missing_file_allows(tmp_path, monkeypatch):
    out, err, code = _run({"args": {"path": str(tmp_path / "ghost.go")}}, monkeypatch)
    _assert_allow(out, code)
    assert "no written file" in err


def test_unmapped_extension_is_noop(tmp_path, monkeypatch):
    f = tmp_path / "notes.txt"
    f.write_text("hi\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "no formatter mapping for '.txt'" in err


def test_no_path_in_event_allows(tmp_path, monkeypatch):
    out, err, code = _run({"args": {}}, monkeypatch)
    _assert_allow(out, code)


# ── path resolution ─────────────────────────────────────────────────────────────────────────


def test_resolve_path_prefers_keys_in_order(tmp_path):
    p = fow.resolve_path({"args": {"path": "/x/a.ts", "file_path": "/y/b.ts"}})
    assert p == Path("/x/a.ts")
    p2 = fow.resolve_path({"args": {"filePath": "/z/c.ts"}})
    assert p2 == Path("/z/c.ts")
    p3 = fow.resolve_path({"path": "/top/d.ts"})
    assert p3 == Path("/top/d.ts")


def test_resolve_relative_path_against_cwd():
    p = fow.resolve_path({"args": {"path": "src/a.ts"}, "cwd": "/proj"})
    assert p == Path("/proj/src/a.ts")


def test_resolve_path_none_when_blank_or_non_string():
    assert fow.resolve_path({"args": {"path": ""}}) is None
    assert fow.resolve_path({"args": {"path": 123}}) is None


# ── detection table ─────────────────────────────────────────────────────────────────────────


def test_table_js_ts_candidate_order():
    cands = fow.TABLE[".ts"]
    detectors = [d.__name__ for d, _ in cands]
    assert detectors == ["_oxfmt_detect", "_prettier_detect", "_biome_detect"]


def test_table_python_candidate_order():
    cands = fow.TABLE[".py"]
    # ruff first, then black
    first_argv = cands[0][1]("ruff", "x.py")
    assert first_argv == ["ruff", "format", "x.py"]
    second_argv = cands[1][1]("black", "x.py")
    assert second_argv == ["black", "-q", "x.py"]


def test_table_go_and_rust():
    assert fow.TABLE[".go"][0][1]("gofmt", "x.go") == ["gofmt", "-w", "x.go"]
    assert fow.TABLE[".rs"][0][1]("rustfmt", "x.rs") == ["rustfmt", "x.rs"]


def test_local_bin_preferred_over_global(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    binp = _install_fake_local_bin(root, "oxfmt")
    # pretend a global oxfmt ALSO exists — local must still win
    monkeypatch.setattr(fow, "has_global", lambda tool: True)
    assert fow._oxfmt_detect(root) == str(binp)


def test_package_json_mention_gated_by_availability(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    (root / "package.json").write_text('{"devDependencies":{"prettier":"^3"}}')
    # mentioned but NOT available → not selected
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    assert fow._prettier_detect(root) is None
    # mentioned AND available → selected (bare global name)
    monkeypatch.setattr(fow, "has_global", lambda tool: True)
    assert fow._prettier_detect(root) == "prettier"


def test_no_configured_formatter_is_noop(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    # neither local bins nor globals exist
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "no configured/available formatter" in err


# ── end-to-end with a fake local formatter ─────────────────────────────────────────────────


def test_runs_local_formatter_on_written_file(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    src = root / "src"
    src.mkdir()
    f = src / "a.ts"
    f.write_text("const   x=1\n")
    # fake oxfmt: rewrite the (last-arg) file to a marker so we can prove it ran
    _install_fake_local_bin(
        root, "oxfmt", body='eval "f=\\${$#}"\nprintf MARKER > "$f"\nexit 0\n'
    )
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "formatted a.ts with oxfmt" in err
    assert f.read_text() == "MARKER"  # the local bin was the one invoked


def test_formatter_nonzero_exit_still_allows(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(root, "oxfmt", body='echo "boom" >&2\nexit 2\n')
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)  # never blocks, even on a formatter error
    assert "exited 2" in err


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
