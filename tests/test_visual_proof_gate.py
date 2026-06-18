"""Tests for the visual-proof-gate agent-hook (pre-bash, commit gate).

Covers the doctrine's four cases. Hook 5 has no subagent exemption; the third case is the
SATISFIED-MARKER path (a fresh "looked at a screenshot" marker => allow). So:
  BLOCK   — a commit with staged user-visible files and no fresh marker.
  ALLOW   — a commit with NO user-visible files staged (nothing to prove).
  MARKER  — staged visual files but a fresh proof marker => allow.
  ESCAPE  — env+reason and inline sentinel allow; reasonless still blocks.

Hermetic: a real tiny git repo is created in tmp_path so the hook's own `git diff --cached
--name-only` subprocess runs for real (no monkeypatching the lister); the proof-marker dir is
redirected into tmp_path. The `git diff` fail-OPEN path is also tested with a non-repo cwd.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_visual_proof_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "visual-proof-gate"
    / "visual_proof_gate.py"
)
_spec = importlib.util.spec_from_file_location("visual_proof_gate", _HOOK)
assert _spec and _spec.loader
vpg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vpg)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=30)


def _mk_repo_with_staged(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        _git(repo, "add", rel)
    return repo


def _run(command, cwd, monkeypatch, *, proof_dir: Path,
         env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(cwd), "args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(vpg, "PROOF_DIR", proof_dir)
    for k in ("ALLOW_NO_VISUAL_PROOF", "ALLOW_NO_VISUAL_PROOF_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = vpg.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _touch_proof(proof_dir: Path) -> None:
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / "looked").write_text("x")


# ── BLOCK ──────────────────────────────────────────────────────────────────────────────

def test_block_commit_with_staged_component_and_no_proof(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_block_commit_with_staged_css_under_components_dir(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "components/card.css")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW (no user-visible files staged) ───────────────────────────────────────────────

def test_allow_commit_with_no_visual_files(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.ts", "README.md")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_allow_non_commit_command(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git status", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_fail_open_when_cwd_is_not_a_git_repo(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    out, _e, c = _run("git commit -m x", not_a_repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


# ── SATISFIED MARKER ───────────────────────────────────────────────────────────────────

def test_allow_when_proof_marker_fresh(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _touch_proof(proof)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


# ── ESCAPE ─────────────────────────────────────────────────────────────────────────────

def test_escape_env_reason_allows(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(
        "git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
        env={"ALLOW_NO_VISUAL_PROOF": "1", "ALLOW_NO_VISUAL_PROOF_REASON": "css var rename"},
    )
    assert c == 0 and _decision(out) == "allow"


def test_escape_inline_sentinel_allows(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x  # visual-proof-ok: deleting a dead component",
                      repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_reasonless_override_still_blocks(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
                      env={"ALLOW_NO_VISUAL_PROOF": "1"})  # no reason
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B2: the GIT_COMMIT regex must NOT match `git`+`commit` in plain prose ────────────────

def test_git_commit_prose_is_not_a_commit(tmp_path, monkeypatch):
    """`echo "... git ... commit"` is not a commit invocation → allow even with staged UI."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run('echo "remember to git, then commit"', repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_git_with_global_flags_commit_is_still_gated(tmp_path, monkeypatch):
    """`git -C path commit` (global flag before subcommand) must still be gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(f"git -C {repo} commit -m x", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── #12: --amend is a real commit (gated); --continue is skipped (allowed) ───────────────

def test_commit_amend_with_staged_ui_is_gated(tmp_path, monkeypatch):
    """`git commit --amend` that re-touches user-visible files still needs proof → BLOCK. An
    amend is a real commit; it must NOT be treated as a skip like --continue/--abort (#12)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --amend --no-edit", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_commit_continue_is_allowed(tmp_path, monkeypatch):
    """`git commit --continue` (mid rebase/merge) carries no new authored change to prove → it
    is in SKIP_COMMIT and allowed even with staged UI files (#12)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --continue", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


# ── T1: NOT subagent-exempt — an agent_id present must STILL block (locks the doctrine) ──

def test_blocks_even_with_agent_id_present(tmp_path, monkeypatch):
    """visual-proof-gate is NOT subagent-exempt: a subagent committing UI work must also have
    looked at the result. An `agent_id` in the event must NOT exempt the commit."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(repo), "agent_id": "sub-x",
             "args": {"command": "git commit -m x", "agent_id": "sub-x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(vpg, "PROOF_DIR", tmp_path / "proof")
    for k in ("ALLOW_NO_VISUAL_PROOF", "ALLOW_NO_VISUAL_PROOF_REASON"):
        monkeypatch.delenv(k, raising=False)
    code = vpg.main()
    assert code == vpg.BLOCK_EXIT_CODE and _decision(out.getvalue()) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
