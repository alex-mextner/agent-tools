"""Tests for ci/ship/refresh-agent-tools-checkout.sh — the periodic self-heal script for the
AGENT_TOOLS_ROOT checkout (agent-tools#315, the general counterpart to ship.sh's own
"agent-tools checkout staleness gate" pre-flight check).

Hermetic: real temp git repos (an origin + a local checkout), no network, no real
AGENT_TOOLS_ROOT touched. Requires bash + git.

    uv run --with pytest python -m pytest tests/test_refresh_agent_tools_checkout.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_REFRESH = Path(__file__).resolve().parents[1] / "ci" / "ship" / "refresh-agent-tools-checkout.sh"


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=dict(os.environ))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def origin_and_checkout(tmp_path):
    """An origin bare repo + a local checkout tracking it on `main`."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", cwd=checkout)
    _git("config", "user.email", "t@t", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)
    _git("remote", "add", "origin", str(origin), cwd=checkout)
    (checkout / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)
    _git("push", "-q", "-u", "origin", "main", cwd=checkout)
    return origin, checkout


def _advance_origin(origin: Path, tmp_path: Path, tag: str) -> str:
    """Push a new commit to origin WITHOUT touching `checkout`. Returns the new SHA."""
    pusher = tmp_path / f"pusher-{tag}"
    _sh("git", "clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=pusher)
    _git("config", "user.name", "t", cwd=pusher)
    (pusher / f"{tag}.txt").write_text(tag, encoding="utf-8")
    _git("add", "-A", cwd=pusher)
    _git("commit", "-qm", tag, cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)
    return _git("rev-parse", "HEAD", cwd=pusher).stdout.strip()


def _run_refresh(checkout: Path):
    return _sh("bash", str(_REFRESH), str(checkout), cwd=checkout, env=dict(os.environ))


def test_pulls_when_clean_on_main_and_behind(origin_and_checkout, tmp_path):
    origin, checkout = origin_and_checkout
    new_sha = _advance_origin(origin, tmp_path, "advance")

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert head == new_sha, f"checkout did not fast-forward: {head} != {new_sha}"


def test_skips_silently_on_feature_branch(origin_and_checkout, tmp_path):
    """Must never switch branches or touch a checkout parked on a feature branch — even
    when main is behind. Regression guard for the exact incident this script exists to
    avoid repeating (checkout once found on a feature branch with real WIP)."""
    origin, checkout = origin_and_checkout
    _advance_origin(origin, tmp_path, "advance")
    _git("checkout", "-qb", "feat/in-progress", cwd=checkout)

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=checkout).stdout.strip()
    assert branch == "feat/in-progress", f"branch changed unexpectedly: {branch}"


def test_skips_silently_when_dirty(origin_and_checkout, tmp_path):
    origin, checkout = origin_and_checkout
    _advance_origin(origin, tmp_path, "advance")
    (checkout / "WIP.md").write_text("uncommitted\n", encoding="utf-8")

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert (checkout / "WIP.md").exists(), "uncommitted file was touched/removed"
    status = _git("status", "--porcelain", cwd=checkout).stdout
    assert "WIP.md" in status, "working tree was silently cleaned"


def test_skips_silently_when_unpushed_commits_ahead(origin_and_checkout, tmp_path):
    origin, checkout = origin_and_checkout
    _advance_origin(origin, tmp_path, "advance")
    (checkout / "local.txt").write_text("local commit\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "unpushed local work", cwd=checkout)
    head_before = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    head_after = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert head_after == head_before, "checkout moved despite unpushed local commits"


def test_noop_when_already_in_sync(origin_and_checkout):
    _origin, checkout = origin_and_checkout
    head_before = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == head_before


def test_exits_zero_when_not_a_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    r = _run_refresh(not_a_repo)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


def test_explicit_nonexistent_path_errors_rather_than_falling_back(tmp_path):
    """An explicit CLI argument that doesn't exist is a likely typo (a mistyped cron line)
    — it must error, not silently redirect to the self-location fallback (that fallback
    exists only for the convenience of the no-argument launchd/cron invocation)."""
    r = _sh("bash", str(_REFRESH), str(tmp_path / "does-not-exist"), cwd=tmp_path, env=dict(os.environ))
    assert r.returncode != 0, f"expected an error for a typo'd path\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


def test_uses_agent_tools_root_env_when_no_arg_given(origin_and_checkout, tmp_path):
    """With no positional argument, $AGENT_TOOLS_ROOT is the checkout to refresh — proven
    by actually advancing origin and asserting the fast-forward happened, not just that the
    script exited 0 (which would pass just as well if the env var were silently ignored)."""
    origin, checkout = origin_and_checkout
    new_sha = _advance_origin(origin, tmp_path, "advance-via-env")
    env = dict(os.environ)
    env["AGENT_TOOLS_ROOT"] = str(checkout)

    r = _sh("bash", str(_REFRESH), cwd=checkout, env=env)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert head == new_sha, f"AGENT_TOOLS_ROOT was not honored: {head} != {new_sha}"


def test_skips_when_no_upstream_configured(tmp_path):
    """A `main` with no `@{upstream}` (e.g. a freshly cloned/init'd repo that never pushed)
    must fail the ahead-count guard cleanly, not crash under `set -e`."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")
    checkout = tmp_path / "no-upstream"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", cwd=checkout)
    _git("config", "user.email", "t@t", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)
    (checkout / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


def _path_without_timeout(tmp_path: Path) -> str:
    """A PATH with the same tools resolvable as normal, EXCEPT `timeout`/`gtimeout` —
    simulates the stock-macOS / minimal-launchd-PATH environment neither exists in."""
    safe_bin = tmp_path / "safe-bin-no-timeout"
    if safe_bin.exists():
        return str(safe_bin)
    safe_bin.mkdir()
    for name in ("bash", "git", "sed", "grep", "awk", "printf", "sort", "wc", "tr",
                 "head", "mktemp", "date", "pgrep", "cut", "cat", "rm", "mkdir", "ls"):
        found = shutil.which(name)
        if found and Path(found).name not in ("timeout", "gtimeout"):
            (safe_bin / name).symlink_to(found)
    return str(safe_bin)


def test_pulls_without_timeout_on_path(origin_and_checkout, tmp_path):
    """The exact regression a full-panel review caught: `timeout` is GNU coreutils, absent
    from a stock macOS PATH. The script must still pull — not silently no-op forever —
    when neither `timeout` nor `gtimeout` is resolvable."""
    origin, checkout = origin_and_checkout
    new_sha = _advance_origin(origin, tmp_path, "advance-no-timeout")
    env = dict(os.environ)
    env["PATH"] = _path_without_timeout(tmp_path)

    r = _sh("bash", str(_REFRESH), str(checkout), cwd=checkout, env=env)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert head == new_sha, f"checkout did not fast-forward without timeout on PATH: {head} != {new_sha}"


def test_recognizes_linked_worktree_checkout(tmp_path):
    """AGENT_TOOLS_ROOT can itself be a LINKED worktree (agent-tools' own convention),
    where `.git` is a FILE, not a directory — the script must still recognize it."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    _git("config", "user.email", "t@t", cwd=primary)
    _git("config", "user.name", "t", cwd=primary)
    _git("remote", "add", "origin", str(origin), cwd=primary)
    (primary / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=primary)
    _git("commit", "-qm", "init", cwd=primary)
    _git("push", "-q", "-u", "origin", "main", cwd=primary)
    linked = tmp_path / "linked-worktree"
    _git("worktree", "add", "-q", "--force", str(linked), "main", cwd=primary)
    assert (linked / ".git").is_file(), "fixture invariant: linked worktree .git must be a FILE"

    new_sha = _advance_origin(origin, tmp_path, "advance-linked")

    r = _run_refresh(linked)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    head = _git("rev-parse", "HEAD", cwd=linked).stdout.strip()
    assert head == new_sha, f"linked worktree did not fast-forward: {head} != {new_sha}"


def test_skips_when_ship_running_from_this_checkout(origin_and_checkout, tmp_path):
    """Best-effort concurrency guard: if a ci/ship/ship.sh from this exact checkout looks
    to be running, don't pull underneath it — avoids rewriting the script file a live `gh
    ship` invocation is mid-way through interpreting."""
    if not shutil.which("pgrep"):
        pytest.skip("pgrep required (guard 5 no-ops without it, elsewhere covered)")
    origin, checkout = origin_and_checkout
    # ci/ship/ship.sh must be COMMITTED (not left untracked) — an untracked file would trip
    # the dirty-tree guard (guard 2) first and exit before guard 5 is ever reached, making
    # this test pass without exercising the pgrep guard at all (the exact vacuous-test bug a
    # review caught in the earlier self-hosting staleness-gate test). Committed+pushed BEFORE
    # the separate "advance" below, so `checkout` stays the ancestor origin fast-forwards from.
    (checkout / "ci" / "ship").mkdir(parents=True)
    fake_ship = checkout / "ci" / "ship" / "ship.sh"
    fake_ship.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "chore: add fake ci/ship/ship.sh", cwd=checkout)
    _git("push", "-q", "origin", "main", cwd=checkout)
    assert _git("status", "--porcelain", cwd=checkout).stdout == "", "fixture invariant: tree must be clean"
    # NOW advance origin further (from a separate clone) — something for the guard to
    # (would-be, absent the pgrep skip) pull, proving a live ship.sh process blocks it.
    _advance_origin(origin, tmp_path, "advance-while-shipping")
    head_before = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()

    pattern = f"{checkout}/ci/ship/ship.sh"
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(15)", str(fake_ship)],
    )
    try:
        # Readiness barrier: subprocess.Popen returns right after fork, before the child's
        # exec has installed the cmdline `pgrep -f` matches against — without waiting for it,
        # this test can flake (pgrep running before exec landed) rather than actually proving
        # the guard works.
        for _ in range(100):
            if _sh("pgrep", "-f", pattern, cwd=checkout).returncode == 0:
                break
            time.sleep(0.05)
        else:
            pytest.fail("fake ship.sh process never became visible to pgrep")

        r = _run_refresh(checkout)
        assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        head_after = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        assert head_after == head_before, "checkout moved while a ship.sh process was running"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_ignored_file_collision_skips_pull(tmp_path):
    """The confirmed regression: git's untracked-file overwrite protection does NOT cover
    IGNORED files — `git status --porcelain` never lists them, so guard 2 alone is blind to
    this. Verified empirically (scratch repo, outside this suite) that a plain
    `git pull --ff-only` silently overwrites an ignored file the incoming commit newly
    tracks. This is the regression test for the fix: the script must refuse to touch the
    checkout when such a collision exists, rather than destroy the local ignored content."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", cwd=checkout)
    _git("config", "user.email", "t@t", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)
    _git("remote", "add", "origin", str(origin), cwd=checkout)
    (checkout / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (checkout / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)
    _git("push", "-q", "-u", "origin", "main", cwd=checkout)

    # Local, gitignored content the script must never destroy.
    (checkout / "secret.txt").write_text("IGNORED LOCAL CONTENT\n", encoding="utf-8")
    assert _git("status", "--porcelain", cwd=checkout).stdout == "", (
        "fixture invariant: an ignored file must not appear in plain `status --porcelain`"
    )

    # origin/main starts TRACKING that same path — the collision.
    pusher = tmp_path / "pusher-collision"
    _sh("git", "clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=pusher)
    _git("config", "user.name", "t", cwd=pusher)
    (pusher / "secret.txt").write_text("NEW TRACKED CONTENT FROM ORIGIN\n", encoding="utf-8")
    _git("add", "-f", "-A", cwd=pusher)
    _git("commit", "-qm", "start tracking secret.txt", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    content = (checkout / "secret.txt").read_text(encoding="utf-8")
    assert content == "IGNORED LOCAL CONTENT\n", (
        f"local ignored content was overwritten by the pull: {content!r}"
    )
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    origin_head = _git("rev-parse", "origin/main", cwd=checkout).stdout.strip()
    assert head != origin_head, "checkout fast-forwarded despite the ignored-file collision"


def test_ignored_dir_collision_with_incoming_file_skips_pull(tmp_path):
    """Ancestor-direction collision (review-cli finding, GH-470): the incoming commit tracks
    a FILE at a path that is currently a directory locally, containing ignored content one
    level down (e.g. incoming file `cache` vs. locally ignored `cache/x`). Exact-path
    equality between the changed path and the ignored path never matches here (`cache` !=
    `cache/x`), so a collision check that only does array membership on exact paths misses
    it entirely — reproduced against the pre-fix code: `git merge --ff-only` silently
    deleted `cache/x` to make room for the incoming file. The fix checks prefix/ancestor
    relationships in both directions, not just equality."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", cwd=checkout)
    _git("config", "user.email", "t@t", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)
    _git("remote", "add", "origin", str(origin), cwd=checkout)
    (checkout / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (checkout / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)
    _git("push", "-q", "-u", "origin", "main", cwd=checkout)

    # Local, gitignored directory-with-content the script must never destroy.
    (checkout / "cache").mkdir()
    (checkout / "cache" / "x").write_text("IGNORED LOCAL CONTENT\n", encoding="utf-8")
    assert _git("status", "--porcelain", cwd=checkout).stdout == "", (
        "fixture invariant: an ignored directory must not appear in plain `status --porcelain`"
    )

    # origin/main starts tracking a FILE (not a dir) at the same path the ignored dir occupies.
    pusher = tmp_path / "pusher-dir-collision"
    _sh("git", "clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=pusher)
    _git("config", "user.name", "t", cwd=pusher)
    (pusher / "cache").write_text("NEW TRACKED FILE FROM ORIGIN\n", encoding="utf-8")
    _git("add", "-f", "-A", cwd=pusher)
    _git("commit", "-qm", "start tracking cache as a file", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    content = (checkout / "cache" / "x").read_text(encoding="utf-8")
    assert content == "IGNORED LOCAL CONTENT\n", (
        f"local ignored content under the directory was overwritten by the pull: {content!r}"
    )
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    origin_head = _git("rev-parse", "origin/main", cwd=checkout).stdout.strip()
    assert head != origin_head, "checkout fast-forwarded despite the ignored-dir collision"


def test_ignored_file_collision_with_incoming_nested_path_skips_pull(tmp_path):
    """Descendant-direction collision (review-cli finding, GH-470): the incoming commit
    tracks a NESTED path (`foo/bar`) where the parent (`foo`) is currently an ignored FILE
    locally — the opposite direction from the ancestor case above. `foo/bar` never equals
    `foo`, so exact-path membership misses this too; a `foo/bar` ff-merge needs `foo` to
    become a directory, clobbering the ignored file at `foo`."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", cwd=checkout)
    _git("config", "user.email", "t@t", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)
    _git("remote", "add", "origin", str(origin), cwd=checkout)
    (checkout / ".gitignore").write_text("foo\n", encoding="utf-8")
    (checkout / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)
    _git("push", "-q", "-u", "origin", "main", cwd=checkout)

    # Local, gitignored FILE the script must never destroy.
    (checkout / "foo").write_text("IGNORED LOCAL CONTENT\n", encoding="utf-8")
    assert _git("status", "--porcelain", cwd=checkout).stdout == "", (
        "fixture invariant: an ignored file must not appear in plain `status --porcelain`"
    )

    # origin/main starts tracking a NESTED path under what is locally an ignored file.
    pusher = tmp_path / "pusher-nested-collision"
    _sh("git", "clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=pusher)
    _git("config", "user.name", "t", cwd=pusher)
    (pusher / "foo").mkdir()
    (pusher / "foo" / "bar").write_text("NEW TRACKED CONTENT FROM ORIGIN\n", encoding="utf-8")
    _git("add", "-f", "-A", cwd=pusher)
    _git("commit", "-qm", "start tracking foo/bar", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)

    r = _run_refresh(checkout)

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    content = (checkout / "foo").read_text(encoding="utf-8")
    assert content == "IGNORED LOCAL CONTENT\n", (
        f"local ignored file was overwritten by the pull: {content!r}"
    )
    head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    origin_head = _git("rev-parse", "origin/main", cwd=checkout).stdout.strip()
    assert head != origin_head, "checkout fast-forwarded despite the ignored-file collision"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
