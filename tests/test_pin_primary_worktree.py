"""Tests for the pin-primary-worktree agent-hook (pre-bash).

Covers the doctrine: in an ENROLLED repo's PRIMARY worktree, `git checkout`/`git switch` to
anything but the default branch is DENIED; the same command inside a LINKED worktree (a real
`git worktree add` tree) is ALLOWED; checking back OUT to the default branch is always allowed;
an un-enrolled repo is never blocked; the escape hatch allows a deliberate switch; a path-restore
form (`checkout -- file`, `checkout .`) is not mistaken for a branch switch; a chain command finds
the offending segment; a `-C <dir>` cross-repo checkout is judged against THAT repo, not cwd.

Exercised against REAL temp git repos and REAL `git worktree add` trees, not mocks — the
git-subprocess primary/linked-worktree distinction is the whole point of this hook.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_pin_primary_worktree.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Isolate throwaway test repos from the developer's GLOBAL git config (SYNC with
# test_worktree_only_writes.py — same rationale: a rig-provisioned core.hooksPath must not
# fire pre-commit gates inside a bare temp repo's setup commands).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "pin-primary-worktree"
    / "pin_primary_worktree.py"
)
_spec = importlib.util.spec_from_file_location("pin_primary_worktree", _HOOK)
assert _spec and _spec.loader
ppw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ppw)

_ENV_KEYS = ("RIG_WORKTREE_ONLY", "RIG_ALLOW_MAIN_EDIT", "RIG_ALLOW_MAIN_EDIT_REASON")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, *, branch: str = "main", enroll: bool = True) -> Path:
    """A real git repo on ``branch`` with one commit and (by default) an enrolling rig.yaml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    if enroll:
        (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "feat/x")  # a feature branch to switch to, not yet checked out
    return repo


def _add_linked_worktree(repo: Path, path: Path, branch: str) -> None:
    _git(repo, "worktree", "add", str(path), branch)


def _run(cwd: Path, command: str, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    event = {"cwd": str(cwd), "args": {"command": command}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = ppw.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: primary worktree, enrolled, checkout to a feature branch ──────────────────────────

def test_primary_checkout_to_feature_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "primary worktree" in json.loads(out)["message"].lower()


def test_primary_switch_to_feature_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git switch feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_primary_checkout_dash_b_new_branch_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout -b feat/new", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_primary_switch_dash_c_new_branch_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git switch -c feat/new", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_env_enrolled_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    out, code = _run(repo, "git checkout feat/x", monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW: switching (back) to the default branch is always fine ────────────────────────────

def test_checkout_back_to_default_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")
    out, code = _run(repo, "git checkout main", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: a LINKED worktree may freely checkout/switch between feature branches ─────────────

def test_linked_worktree_checkout_to_feature_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    linked = tmp_path / "linked"
    _add_linked_worktree(repo, linked, "feat/x")
    _git(linked, "branch", "feat/y")
    out, code = _run(linked, "git checkout feat/y", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: not enrolled (no env, no rig.yaml knob) ───────────────────────────────────────────

def test_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_rigyaml_explicit_false_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: not a branch switch at all ────────────────────────────────────────────────────────

def test_path_restore_with_dashdash_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x -- seed.txt", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_checkout_dot_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout .", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_read_only_git_commands_allow(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for cmd in ("git status", "git log --oneline -5", "git worktree list", "git branch"):
        out, code = _run(repo, cmd, monkeypatch)
        assert code == 0 and _decision(out) == "allow", cmd


def test_worktree_add_allows(tmp_path, monkeypatch):
    """`git worktree add` is the RECOMMENDED alternative — must never itself be blocked."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, f"git worktree add {tmp_path / 'wt2'} feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── the offending segment can be anywhere in a chain ─────────────────────────────────────────

def test_chain_finds_offending_segment(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git status && git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── `-C <dir>` is judged against THAT repo, not the shell cwd ────────────────────────────────

def test_dash_c_cross_repo_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out, code = _run(elsewhere, f"git -C {repo} checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dash_c_cross_repo_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")  # cwd IS enrolled…
    out, code = _run(elsewhere, f"git -C {repo} checkout feat/x", monkeypatch)  # …but repo is not
    assert code == 0 and _decision(out) == "allow"


# ── ESCAPE hatch ──────────────────────────────────────────────────────────────────────────────

def test_escape_hatch_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x", monkeypatch, {"RIG_ALLOW_MAIN_EDIT": "1"})
    assert code == 0 and _decision(out) == "allow"
    assert "escape hatch" in json.loads(out)["message"].lower()


# ── `git checkout -` (previous branch) is resolved via @{-1}, not string-blind ───────────────

def test_checkout_dash_previous_branch_denies_when_feature(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")
    _git(repo, "checkout", "main")  # @{-1} now resolves to feat/x
    out, code = _run(repo, "git checkout -", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_checkout_dash_previous_branch_allows_when_default(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")  # main -> feat/x; @{-1} is now "main" (the default)
    out, code = _run(repo, "git checkout -", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── fail-OPEN: not a git repo, bad event, empty command ─────────────────────────────────────

def test_non_git_dir_allows(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out, code = _run(plain, "git checkout feat/x", monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_bad_event_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    assert ppw.main() == 0 and _decision(out.getvalue()) == "allow"


def test_empty_command_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── the bridge execs `cmd` DIRECTLY (subprocess.run([cmd, ...])) — a missing shebang is an
# exec-format error resolved via on_error=open, silently ALLOWING every checkout this hook
# exists to block. `importlib`-loading (every test above) can't catch this — only a real
# subprocess exec of the file exercises the actual bridge invocation shape ──────────────────────

def test_hook_is_directly_executable(tmp_path):
    """Regression for the missing-shebang bug: run the hook file itself as an executable
    (exactly how ``cc_hook_bridge/dispatch.py`` invokes it), not via ``import``. Before the fix
    this file started with a bare docstring, no ``#!``, so the OS fell back to running it as a
    shell script — the docstring/code was executed as shell commands (observed: it literally
    ran a stray `git checkout <branch>` parsed out of the docstring text)."""
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo), "args": {"command": "git checkout feat/x"}}
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    proc = subprocess.run(
        [str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert proc.returncode == ppw.BLOCK_EXIT_CODE, (proc.returncode, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["decision"] == "block"


# ── a leading shell VAR=val assignment must not defeat classification ───────────────────────

def test_leading_unrelated_env_assignment_still_denies(tmp_path, monkeypatch):
    """`GIT_TRACE=1 git checkout feat/x` — an unrelated leading assignment — must still be
    recognized and BLOCKED, not silently allowed because `toks[0] != "git"`."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "GIT_TRACE=1 git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_leading_escape_hatch_set_to_zero_still_denies(tmp_path, monkeypatch):
    """`RIG_ALLOW_MAIN_EDIT=0 git checkout feat/x` — an explicit non-"1" value inline — must
    still deny; the classification fix must not accidentally treat ANY leading assignment as
    granting the escape hatch."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "RIG_ALLOW_MAIN_EDIT=0 git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_inline_escape_hatch_allows(tmp_path, monkeypatch):
    """The README's documented one-liner shape — `RIG_ALLOW_MAIN_EDIT=1 ... git checkout x` —
    must actually grant the escape hatch, even though this hook's own os.environ never sees an
    inline shell assignment (it runs as a separate process before bash executes the command)."""
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo,
        'RIG_ALLOW_MAIN_EDIT=1 RIG_ALLOW_MAIN_EDIT_REASON="deliberate, worktree overkill" '
        "git checkout feat/x",
        monkeypatch,
    )
    assert code == 0 and _decision(out) == "allow"
    msg = json.loads(out)["message"].lower()
    assert "escape hatch" in msg
    assert "deliberate, worktree overkill" in msg


def test_inline_escape_hatch_overrides_env_allow_to_deny(tmp_path, monkeypatch):
    """An explicit inline `RIG_ALLOW_MAIN_EDIT=0` re-asserts the block even when the AMBIENT
    process env already has the escape hatch on — inline is a per-command override, matching
    shell `VAR=val cmd` precedence."""
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo,
        "RIG_ALLOW_MAIN_EDIT=0 git checkout feat/x",
        monkeypatch,
        {"RIG_ALLOW_MAIN_EDIT": "1"},
    )
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
