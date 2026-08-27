"""Tests for the declared-generated-data exemption in require-review-before-commit.

A machine-generated data dump (the motivating case: a 5.9 MB Figma node tree) has a `.json`
extension, so `CODE_EXT` classifies it as code and demands a review it can never receive — the
diff runs to ~221k lines and exceeds any reviewer's context, making the gate *unsatisfiable*
rather than strict.

The exemption is deliberately narrow. Three conditions must hold together:
  1. the path is under a `docs/` directory,
  2. `git check-attr diff` reports `unset` (i.e. `-diff`, or the `binary` macro, is declared),
  3. the extension is a pure serialisation format (never a script, lockfile, env or infra file).

The declaration lives in `.gitattributes`, which is NOT itself a docs path — so granting the
exemption is a reviewed commit. That is the control. These tests assert the ALLOW case and, more
importantly, the four ways it must still BLOCK.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_require_review_generated_data.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "require-review-before-commit"
    / "require_review.py"
)
_spec = importlib.util.spec_from_file_location("require_review_gd", _HOOK)
assert _spec and _spec.loader
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

_FIGMA_ATTR = "docs/figma/*.json -diff"


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=30)


def _stage_with_attrs(repo: Path, attrs: str | None, *files: str) -> Path:
    """A git repo with `files` staged and, when `attrs` is given, a COMMITTED `.gitattributes`.

    `.gitattributes` is committed rather than staged so it never appears in the staged diff — the
    test then measures only how `files` themselves are classified."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # Isolate from any global core.hooksPath so fixture commits never run real hooks.
    _git(repo, "config", "core.hooksPath", "/dev/null")
    if attrs is not None:
        (repo / ".gitattributes").write_text(attrs + "\n")
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-q", "-m", "attrs")
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        _git(repo, "add", rel)
    return repo


def _run(command: str, cwd: Path, monkeypatch, *, marker: Path) -> tuple[str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(cwd), "args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("REVIEW_MARKER", str(marker))
    monkeypatch.delenv("REVIEW_SKIP", raising=False)
    monkeypatch.delenv("RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT", raising=False)
    # `main()` must run BEFORE stdout is read — a `return out.getvalue(), rr.main()` would
    # evaluate left-to-right and capture an empty buffer.
    code = rr.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _assert_allow(out: str, code: int) -> None:
    assert code == 0 and _decision(out) == "allow"


def _assert_block(out: str, code: int) -> None:
    assert code == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW: the motivating case ───────────────────────────────────────────────────────────

def test_allow_declared_generated_data_under_docs(tmp_path, monkeypatch):
    """A Figma node dump declared `-diff` under docs/ commits without a review marker."""
    repo = _stage_with_attrs(tmp_path / "repo", _FIGMA_ATTR, "docs/figma/nodes-breakpoints.json")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_allow(out, code)


def test_allow_declared_data_mixed_with_ordinary_docs(tmp_path, monkeypatch):
    """Declared data alongside prose is still exempt — every staged path is exempt."""
    repo = _stage_with_attrs(tmp_path / "repo", _FIGMA_ATTR,
                             "docs/figma/nodes-colors.json", "docs/figma/README.md")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_allow(out, code)


# ── BLOCK: the exemption must not become a general escape ────────────────────────────────

def test_block_undeclared_json_under_docs(tmp_path, monkeypatch):
    """Without the `-diff` declaration the same file still needs review. Location alone grants
    nothing — the declaration is the control."""
    repo = _stage_with_attrs(tmp_path / "repo", None, "docs/figma/nodes-breakpoints.json")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_block_declared_json_outside_docs(tmp_path, monkeypatch):
    """`-diff` on a path outside docs/ grants nothing — no route around reviewing real config."""
    repo = _stage_with_attrs(tmp_path / "repo", "package.json -diff", "package.json")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_block_declared_script_under_docs(tmp_path, monkeypatch):
    """An executable format is never exemptable, however it is declared."""
    repo = _stage_with_attrs(tmp_path / "repo", "docs/build.py -diff", "docs/build.py")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_block_declared_lockfile_under_docs(tmp_path, monkeypatch):
    """Supply-chain files stay reviewable even under docs/ with `-diff` declared."""
    repo = _stage_with_attrs(tmp_path / "repo", "docs/bun.lock -diff", "docs/bun.lock")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_generated_data_check_fails_closed(tmp_path, monkeypatch):
    """If the attribute lookup cannot answer, the file is NOT exempt — a broken or slow `git`
    must never degrade into a silent review skip."""
    repo = _stage_with_attrs(tmp_path / "repo", _FIGMA_ATTR, "docs/figma/nodes-colors.json")
    monkeypatch.setattr(rr, "_declared_nondiffable_batch", lambda _p, _c: set())
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_block_when_declaration_is_uncommitted(tmp_path, monkeypatch):
    """The control is a REVIEWED commit, so a working-tree-only `.gitattributes` must not arm the
    exemption. Attributes are read from HEAD precisely so this cannot work."""
    repo = _stage_with_attrs(tmp_path / "repo", None, "docs/figma/nodes-breakpoints.json")
    (repo / ".gitattributes").write_text(_FIGMA_ATTR + "\n")  # on disk, never committed
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


def test_block_when_local_info_attributes_exists(tmp_path, monkeypatch):
    """`$GIT_DIR/info/attributes` outranks in-tree attributes and is never committed, so its mere
    presence disqualifies the exemption even for a legitimately declared path."""
    repo = _stage_with_attrs(tmp_path / "repo", _FIGMA_ATTR, "docs/figma/nodes-breakpoints.json")
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("# any local override at all\n")
    out, code = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    _assert_block(out, code)


# ── Unit level ───────────────────────────────────────────────────────────────────────────

def test_declared_nondiffable_batch_reads_committed_attributes(tmp_path):
    """The batch helper reflects real committed attribute state, and answers for every path in a
    single call."""
    repo = _stage_with_attrs(tmp_path / "repo", _FIGMA_ATTR, "docs/figma/nodes-colors.json")
    got = rr._declared_nondiffable_batch(
        ["docs/figma/nodes-colors.json", "docs/notes/data.json"], str(repo)
    )
    assert got == {"docs/figma/nodes-colors.json"}


def test_is_generated_data_path_requires_all_three_conditions(tmp_path):
    """Each condition is load-bearing: location, declaration, and extension."""
    repo = _stage_with_attrs(
        tmp_path / "repo",
        "docs/figma/*.json -diff\ndocs/build.py -diff\nsrc/app.json -diff",
        "docs/figma/nodes-colors.json",
    )
    cwd = str(repo)
    assert rr.is_generated_data_path("docs/figma/nodes-colors.json", cwd) is True
    assert rr.is_generated_data_path("docs/build.py", cwd) is False       # extension
    assert rr.is_generated_data_path("src/app.json", cwd) is False        # location
    # `docs/notes/data.json` is under docs/ with an allowed extension but is NOT covered by any
    # `-diff` pattern — the declaration is what is missing, and that alone is disqualifying.
    assert rr.is_generated_data_path("docs/notes/data.json", cwd) is False
