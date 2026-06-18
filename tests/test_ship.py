"""Tests for ci/ship/ship.sh — the post-merge worktree cleanup.

Focus: the exit-128 regression where TWO worktrees checked out the same branch and the
script concatenated both paths into one `git worktree remove`. ship.sh must:
  • collect ALL worktrees for the branch and remove each (not crash on the second);
  • report the merge as done even when cleanup hits a problem, never masking a
    successful squash-merge behind a non-zero exit.

Hermetic: a real temp git repo with two worktrees on one branch + a fake `gh` on PATH
(so no network / real PR). ship.sh's own merge step is the fake `gh pr merge` (a no-op);
the worktree/branch cleanup runs for real against the temp repo.

Requires bash + git. Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# A fake `gh` that answers exactly the calls ship.sh makes (with --skip-ci the CI rollup
# is not queried). Branch name is read from $SHIP_TEST_BRANCH so the test controls it.
_FAKE_GH = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        # --json headRefName,state,mergeable,isCrossRepository,mergeStateStatus
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;            # --name-only (non-UI path)
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;                          # graphql review-threads -> 0 unresolved
  *) : ;;
esac
"""


def _clean_git_env() -> dict:
    """A git env immune to the developer box's GLOBAL hooks / config: an empty
    core.hooksPath (so a host `review`-install pre-commit can't block the test's own
    commits) and REVIEW_SKIP set for belt-and-suspenders."""
    env = dict(os.environ)
    env["REVIEW_SKIP"] = "1"
    return env


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    # Neutralize any global core.hooksPath for the test's bootstrap commits.
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=_clean_git_env())
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo_with_two_worktrees(tmp_path):
    """A repo on `main` with a branch `feat` checked out in TWO worktrees (the exit-128
    trigger), plus an `origin` remote so ship.sh's remote-branch ops have a target."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    # Branch `feat`, pushed, then checked out in TWO separate worktrees.
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    _git("worktree", "add", "-q", str(wt1), "feat", cwd=main)
    # A second worktree on the SAME branch: git allows it with --force.
    _git("worktree", "add", "-q", "--force", str(wt2), "feat", cwd=main)
    return main, wt1, wt2


def _run_ship(main: Path, bindir: Path):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    return _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )


def _fake_gh_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def test_two_worktrees_one_branch_does_not_exit_128(repo_with_two_worktrees, tmp_path):
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)

    r = _run_ship(main, bindir)

    # The exact regression: two worktrees used to make cleanup exit 128.
    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Both worktrees for the branch must be gone (removed individually).
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt1) not in remaining, f"wt1 still present:\n{remaining}"
    assert str(wt2) not in remaining, f"wt2 still present:\n{remaining}"


def test_merge_reported_even_if_cleanup_cannot_remove_branch(repo_with_two_worktrees, tmp_path):
    """If a worktree can't be removed (e.g. left checked out / busy), ship must still exit
    0 and report the merge — cleanup failure must NOT mask a successful merge."""
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)

    # Corrupt wt2 so `git worktree remove` (and --force) both fail, simulating a stuck
    # removal. ship.sh must still exit 0 and report the merge.
    gitfile = wt2 / ".git"
    if gitfile.exists():
        gitfile.write_text("gitdir: /nonexistent/broken\n", encoding="utf-8")

    r = _run_ship(main, bindir)

    assert r.returncode == 0, f"ship must exit 0 after a successful merge; got {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Assert the MASKING-prevention path actually fired — otherwise this test could go
    # false-green on a git where `worktree remove --force` happens to drop the broken tree
    # (the cleanup would then succeed and never exercise the warn-don't-abort branch).
    assert "could not remove worktree" in r.stderr, (
        "the un-removable-worktree path did not fire — test proves nothing:\n" + r.stderr
    )


def test_worktree_path_with_space_is_collected(tmp_path):
    """The fix swapped awk `$2` (splits on whitespace) for `substr($0,10)` so a worktree
    whose path contains a space is parsed whole. With `$2` the path was truncated and the
    branch's worktree went uncollected — cleanup then left it behind (and `git -C` on the
    truncated path exit-128'd). Here a single worktree under a spaced path must be removed."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)

    wt_spaced = tmp_path / "work tree with spaces"
    _git("worktree", "add", "-q", str(wt_spaced), "feat", cwd=main)

    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship(main, bindir)

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # The spaced-path worktree must have been collected and removed — proof the full path
    # (not a `$2`-truncated prefix) was parsed.
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt_spaced) not in remaining, f"spaced worktree still present:\n{remaining}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
