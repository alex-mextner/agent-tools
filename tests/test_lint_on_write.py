"""Tests for the lint-on-write agent-hook.

Hermetic: no network, no real linters. The "does it run a linter" cases install a tiny
fake executable into a temp repo's node_modules/.bin (so the local-bin preference is
exercised); findings resolve to exit 10 + a `block` protocol message (the bridge turns
that into PostToolUse FEEDBACK), every failure mode resolves to `allow`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_lint_on_write.py -q
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
    / "lint-on-write"
    / "lint_on_write.py"
)
_spec = importlib.util.spec_from_file_location("lint_on_write", _HOOK)
assert _spec and _spec.loader
low = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(low)


def _run(event, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Drive main() with `event` on stdin; return (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = low.main()
    return out.getvalue(), err.getvalue(), code


def _assert_allow(out: str, code: int) -> None:
    assert code == 0
    payload = json.loads(out)
    assert payload == {"hook_api": low.HOOK_API, "decision": "allow"}
    assert out.endswith("\n"), "protocol line must be newline-terminated"


def _assert_block(out: str, code: int) -> dict:
    assert code == 10, "findings must use the canonical v1 BLOCK exit"
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert payload["hook_api"] == low.HOOK_API
    return payload


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _install_fake_local_bin(root: Path, tool: str, body: str = "exit 0\n") -> Path:
    binp = root / "node_modules" / ".bin" / tool
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("#!/bin/sh\n" + body)
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binp


# ── escape hatch + always-allow failure modes ─────────────────────────────────────────


def test_escape_hatch_skips_before_touching_anything(tmp_path, monkeypatch):
    f = tmp_path / "a.ts"
    f.write_text("const x=1\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch, {"NO_LINT_HOOK": "1"})
    _assert_allow(out, code)
    assert "NO_LINT_HOOK=1" in err


def test_non_dict_event_allows(tmp_path, monkeypatch):
    out, err, code = _run([1, 2, 3], monkeypatch)  # type: ignore[arg-type]
    _assert_allow(out, code)
    assert "not a JSON object" in err


def test_missing_file_allows(tmp_path, monkeypatch):
    out, err, code = _run({"args": {"path": str(tmp_path / "ghost.ts")}}, monkeypatch)
    _assert_allow(out, code)
    assert "no written file" in err


def test_unmapped_extension_is_noop(tmp_path, monkeypatch):
    """Non-source files (docs, data, styles) are never linted — that keeps every doc edit
    free of linter startup cost. .json/.md are deliberately NOT in the table."""
    for name in ("notes.txt", "data.json", "README.md", "style.css"):
        f = tmp_path / name
        f.write_text("hi\n")
        out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
        _assert_allow(out, code)
        assert "no linter mapping" in err


def test_generated_paths_are_skipped(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "node_modules" / "pkg" / "index.ts"
    f.parent.mkdir(parents=True)
    f.write_text("const x=1\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "generated/vendored" in err


def test_vendored_skip_is_scoped_to_repo_relative_path(tmp_path, monkeypatch):
    """A checkout sitting under a parent dir that shares a skip-segment name (e.g. a
    worktree at `/tmp/build/myrepo`) must not have every file in the repo skipped — only
    paths vendored *within* the repo itself. Regression: the check used to run against the
    full absolute path, matching on ancestor segments outside the repo root."""
    assert "build" in low.SKIP_SEGMENTS, "test premise: the ancestor dir name must actually be a skip segment"
    root = tmp_path / "build" / "myrepo"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    f = root / "src" / "a.ts"
    f.parent.mkdir(parents=True)
    f.write_text("const x=1\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "generated/vendored" not in err


def test_repo_root_not_a_prefix_falls_back_without_crash(tmp_path, monkeypatch):
    """A `repo_root` that isn't a literal path-string prefix of the written file (a case
    that shouldn't arise given `repo_root` never resolves symlinks, but is cheap to guard)
    must not crash the hook — fall back to the full path rather than raise."""
    root = _mk_repo(tmp_path)
    f = root / "src" / "a.ts"
    f.parent.mkdir(parents=True)
    f.write_text("const x=1\n")
    monkeypatch.setattr(low, "repo_root", lambda start: Path("/definitely/not/a/prefix"))
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)


def test_nested_git_root_defeats_vendored_skip_known_limitation(tmp_path, monkeypatch):
    """Characterization test for a known, deliberately-unfixed limitation (see the comment
    on `repo_root`): it returns the NEAREST enclosing `.git`, so a vendored dir that itself
    carries a `.git` — including a git submodule, whose `.git` is a file pointer that
    `find_up` matches the same as a real `.git` dir — has that segment scoped away, and a
    file inside it is no longer skipped as vendored. This pins TODAY's behavior so a future
    change to `repo_root`/`find_up` doesn't silently shift the boundary unnoticed."""
    assert "vendor" in low.SKIP_SEGMENTS, "test premise: the vendored dir name must actually be a skip segment"
    outer = _mk_repo(tmp_path)
    nested = outer / "vendor" / "somelib"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: ../../.git/modules/somelib\n")  # submodule gitfile
    f = nested / "file.ts"
    f.write_text("const x=1\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "generated/vendored" not in err


# ── detection table ────────────────────────────────────────────────────────────────────


def test_table_js_ts_candidate_order():
    detectors = [d.__name__ for d, _ in low.TABLE[".ts"]]
    assert detectors == ["_oxlint_detect", "_biome_detect", "_eslint_detect"]


def test_table_argv_single_file_scope():
    """Every candidate lints exactly the one written file — never the whole repo."""
    assert low.TABLE[".ts"][0][1]("oxlint", "x.ts") == ["oxlint", "x.ts"]
    assert low.TABLE[".ts"][1][1]("biome", "x.ts") == ["biome", "lint", "x.ts"]
    assert low.TABLE[".ts"][2][1]("eslint", "x.ts") == ["eslint", "--no-warn-ignored", "x.ts"]
    assert low.TABLE[".py"][0][1]("ruff", "x.py") == ["ruff", "check", "x.py"]


def test_ruff_global_requires_config(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    monkeypatch.setattr(low, "has_global", lambda tool: tool == "ruff")
    # no ruff config → a globally-installed ruff is NOT the repo's linter
    assert low._ruff_detect(root) is None
    # ruff.toml (exactly what rig's `linters` area writes) turns it on
    (root / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    assert low._ruff_detect(root) == "ruff"


def test_eslint_global_requires_config_or_mention(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    monkeypatch.setattr(low, "has_global", lambda tool: tool == "eslint")
    assert low._eslint_detect(root) is None
    (root / "eslint.config.mjs").write_text("export default []\n")
    assert low._eslint_detect(root) == "eslint"


def test_oxlint_config_file_enables_global(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    monkeypatch.setattr(low, "has_global", lambda tool: tool == "oxlint")
    assert low._oxlint_detect(root) is None
    (root / ".oxlintrc.json").write_text("{}\n")
    assert low._oxlint_detect(root) == "oxlint"


def test_local_bin_preferred_over_global(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    binp = _install_fake_local_bin(root, "oxlint")
    monkeypatch.setattr(low, "has_global", lambda tool: True)
    assert low._oxlint_detect(root) == str(binp)


def test_venv_bin_found_for_python(tmp_path):
    root = _mk_repo(tmp_path)
    binp = root / ".venv" / "bin" / "ruff"
    binp.parent.mkdir(parents=True)
    binp.write_text("#!/bin/sh\nexit 0\n")
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC)
    assert low.local_bin(root, "ruff") == str(binp)


def test_no_configured_linter_is_noop(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    monkeypatch.setattr(low, "has_global", lambda tool: False)
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "no configured/available linter" in err


# ── end-to-end with a fake local linter ────────────────────────────────────────────────


def test_clean_file_allows(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x = 1\n")
    _install_fake_local_bin(root, "oxlint", body="exit 0\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "clean" in err


def test_findings_block_with_linter_output(tmp_path, monkeypatch):
    """Linter exit 1 → v1 BLOCK (exit 10) whose message carries the findings + the path,
    so the bridge can surface actionable feedback to the agent."""
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(
        root, "oxlint", body='echo "a.ts:1:7 no-unused-vars: x is unused"\nexit 1\n'
    )
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    payload = _assert_block(out, code)
    assert "no-unused-vars" in payload["message"]
    assert str(f) in payload["message"]
    assert "NO_LINT_HOOK" in payload["message"]  # the escape hatch is advertised


def test_linter_tool_error_allows(tmp_path, monkeypatch):
    """Exit >= 2 is a tool/config error, not findings — advisory hook must allow."""
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(root, "oxlint", body='echo "config broken" >&2\nexit 2\n')
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "exited 2" in err


def test_linter_timeout_allows(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(root, "oxlint", body="sleep 60\n")
    monkeypatch.setattr(low, "RUN_TIMEOUT_S", 0.2)
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "failed to run" in err


def test_findings_output_is_truncated(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(root, "oxlint", body="seq 1 500\nexit 1\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    payload = _assert_block(out, code)
    assert "output truncated" in payload["message"]
    assert len(payload["message"]) < 4000


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
