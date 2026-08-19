"""Tests for ci/ship/ship.sh's post-merge task-cli notify hook (`_ship_notify_task_cli`).

Kept in its own file (not tests/test_ship.py) so this feature doesn't collide with the many
other in-flight worktrees editing that file — same hermetic approach: a real temp git repo +
a fake `gh` on PATH (no network) + a fake `task` binary that logs its own invocation so the
test can assert exactly what ship called it with, without a real task-cli install.

Requires bash + git. Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_notify_task_cli.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# See tests/test_ship.py's identical rationale: the review-quorum gate defaults ENABLED in the
# product but this file never exercises it (no `review`/`jq` quorum fixture here) — force it off
# process-wide; nothing here re-enables it.
os.environ.setdefault("SHIP_REVIEW_QUORUM", "0")

# A minimal fake `gh` answering exactly the calls ship.sh makes on the notify path: the
# preflight (headRefName,...), the CI rollup (green, so the normal merge path is taken), the
# unresolved-review-threads graphql query (0), the merge itself, and the TWO batched `pr view`
# queries the notify hook adds (title+body combined, url+mergeCommit combined — ship.sh fetches
# each pair in a single `gh pr view --json a,b` call, not two separate ones). Each source is
# controlled by an env var so a test can pick exactly which one carries the ticket code, and
# whether a merge commit SHA is available. When SHIP_TEST_GH_CALL_LOG is set, every `pr view`
# call's raw args are appended there (one line each) so a test can assert the call COUNT.
_FAKE_GH = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        args="$*"
        [ -n "${SHIP_TEST_GH_CALL_LOG:-}" ] && printf '%s\\n' "$args" >> "${SHIP_TEST_GH_CALL_LOG}"
        if printf '%s' "$args" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s' "$args" | grep -q statusCheckRollup; then
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
        elif printf '%s' "$args" | grep -q -- '--json title,body'; then
          printf '%s\\t%s\\n' "${SHIP_TEST_PR_TITLE:-}" "${SHIP_TEST_PR_BODY:-}"
        elif printf '%s' "$args" | grep -q -- '--json url,mergeCommit'; then
          printf '%s\\t%s\\n' "${SHIP_TEST_PR_URL-https://github.com/acme/widgets/pull/1}" "${SHIP_TEST_MERGE_SHA:-}"
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    # The only `gh api` call reached on this path (dwell is disabled via SHIP_REVIEW_DWELL=0):
    # the unresolved-review-threads query. Always answer 0 unresolved.
    echo "${SHIP_TEST_UNRESOLVED:-0}" ;;
  *) : ;;
esac
"""

# A fake `task` that logs its own argv (one line, space-joined) to $TASK_LOG and exits
# $SHIP_TEST_TASK_EXIT (default 0) — lets a test assert exactly what ship called it with, and
# exercise "task itself fails" without a real task-cli install.
_FAKE_TASK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${TASK_LOG}"
exit "${SHIP_TEST_TASK_EXIT:-0}"
"""


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=dict(os.environ))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo_with_pr_worktree(tmp_path):
    """A repo on `main` with branch `feat` checked out in one worktree, plus an `origin`
    remote — the same shape tests/test_ship.py's fixture of the same name uses."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

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
    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=main)
    return main, wt


def _bindir(tmp_path: Path, *, with_task: bool) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    if with_task:
        task = bindir / "task"
        task.write_text(_FAKE_TASK, encoding="utf-8")
        task.chmod(0o755)
    return bindir


def _path_without_real_task() -> str:
    """The ambient PATH with EVERY directory that contains a real `task` binary stripped out —
    not just the first `shutil.which` hit — so a "task not on PATH" test isn't accidentally
    satisfied by a developer machine that has more than one task-cli install shadowing each
    other on PATH (this machine has both ~/.local/bin/task and ~/.files/bin/task), which would
    make the test pass for the wrong reason (or fail outright, since a REAL task-cli doesn't
    understand this test's fake ticket codes/args)."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in dirs if not (d and Path(d, "task").exists()))


def _run_ship(main: Path, bindir: Path, tmp_path: Path, *, branch="feat", extra_env=None, extra_args=(), base_path=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{base_path if base_path is not None else env['PATH']}"
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_DWELL"] = "0"
    env["TASK_LOG"] = str(tmp_path / "task.log")
    env["SHIP_TEST_GH_CALL_LOG"] = str(tmp_path / "gh-pr-view.log")
    # Explicitly override the process-wide default other test modules in this suite may have
    # already applied (tests/test_ship.py sets SHIP_TASK_NOTIFY_ENABLED=0 via os.environ.setdefault
    # so its own un-stubbed fixtures never invoke a real task-cli) — this file's whole point is
    # to exercise the notify feature itself, always with its OWN fake `task` on PATH.
    env["SHIP_TASK_NOTIFY_ENABLED"] = "1"
    if extra_env:
        env.update(extra_env)
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )
    log_path = Path(env["TASK_LOG"])
    calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return r, calls


def _gh_pr_view_calls(tmp_path: Path) -> list[str]:
    """The `gh pr view ...` argv lines logged by the fake gh for the just-run `_run_ship` call
    (same tmp_path passed to it) — lets a test assert the batched-call-count property."""
    log_path = tmp_path / "gh-pr-view.log"
    return log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []


def test_notify_calls_mark_shipped_with_branch_derived_code(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_PR_URL": "https://github.com/acme/widgets/pull/1"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]
    assert "--pr https://github.com/acme/widgets/pull/1" in calls[0]
    assert "notifying task-cli: HYP-931" in r.stdout


def test_notify_includes_commit_when_merge_commit_present(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_MERGE_SHA": "deadbeef1234"},
    )

    assert r.returncode == 0
    assert len(calls) == 1
    assert "--commit deadbeef1234" in calls[0]


def test_notify_falls_back_to_pr_title_when_branch_has_no_code(repo_with_pr_worktree, tmp_path):
    """The gap the review-quorum gate has (branch/body only, never title) must NOT be
    inherited here: a generic branch name with the ticket code only in the PR title must
    still be found."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "HYP-931: fix the thing", "SHIP_TEST_PR_BODY": "no code here"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_finds_the_title_code_even_when_it_is_not_the_first_word(repo_with_pr_worktree, tmp_path):
    """Regression: a bare `read -r pr_title pr_body_local` (default IFS) splits on ANY
    whitespace, not just the tab `@tsv` inserts — it silently truncated pr_title to its FIRST
    WORD and dumped the rest into pr_body_local. A title like "Fix the thing (HYP-931)" would
    read as pr_title="Fix" (no match), and worse, a title like "UTF-8 fix for HYP-931" would
    give pr_title="UTF-8" — matching the digit-containing-but-wrong arm-2 pattern and reaching
    `task mark-shipped UTF-8` instead of the real HYP-931 (the exact wrong-ticket hazard the
    digit filter exists to catch, except the real code never even reached it). Must split on
    the tab alone (IFS=$'\\t')."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "Fix the thing (HYP-931)", "SHIP_TEST_PR_BODY": "no code here"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_falls_back_to_pr_body_when_branch_and_title_have_no_code(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "refs HYP-931 for details"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_skips_when_no_code_derivable_anywhere(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "no ticket mentioned"},
    )

    assert r.returncode == 0, "a missing task code must never block the ship"
    assert "merged #1" in r.stdout
    assert calls == []
    assert "could not derive a task code" in r.stderr


def test_notify_skipped_when_task_not_on_path(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=False)  # no `task` binary

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", base_path=_path_without_real_task(),
    )

    assert r.returncode == 0
    assert "merged #1" in r.stdout
    assert calls == []
    assert "task-cli not on PATH" in r.stderr


def test_notify_failure_does_not_fail_the_ship(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_TASK_EXIT": "1"},
    )

    assert r.returncode == 0, "a failing task-cli call must never fail an already-successful ship"
    assert "merged #1" in r.stdout
    assert len(calls) == 1  # task WAS invoked; it just failed
    assert "task mark-shipped" in r.stderr and "failed" in r.stderr


def test_notify_skipped_in_dry_run(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_args=["--dry-run"],
    )

    assert r.returncode == 0
    assert calls == []
    assert "would notify task-cli" in r.stdout


def test_notify_skipped_for_a_foreign_repo_invocation(repo_with_pr_worktree, tmp_path):
    """--repo targeting a DIFFERENT repo than this checkout's own origin must never let a
    local `task` write into the wrong project — the one guard standing between this feature
    and a cross-project ticket-store corruption, so it gets its own explicit test (review
    finding: it was previously exercised by nothing)."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_args=["--repo", "acme/other"],
    )

    assert r.returncode == 0
    assert calls == []
    assert "foreign remote" in r.stderr


def test_notify_skipped_when_pr_url_cannot_be_resolved(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_env={"SHIP_TEST_PR_URL": ""},
    )

    assert r.returncode == 0, "an unresolvable PR URL must never block an already-successful merge"
    assert "merged #1" in r.stdout
    assert calls == []
    assert "could not resolve" in r.stderr


def test_notify_rejects_a_digit_free_false_positive_code(repo_with_pr_worktree, tmp_path):
    """A generic PR title can trip the matcher's purely-descriptive arm (e.g. "DO-NOT-MERGE
    experimental spike" -> "DO-NOT-MERGE", a documented accepted false positive of the
    matcher — see ship.sh's own comment on _review_quorum_extract_ticket) — real task-cli
    ticket ids always carry a numeric suffix (HYP-931, #123), so a digit-free "code" must
    never reach `task mark-shipped` (review finding: it would either fail confusingly against
    an unrelated existing ticket, or in the worst case silently mark the WRONG ticket
    "shipped" if one happens to share that literal id)."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "DO-NOT-MERGE experimental spike", "SHIP_TEST_PR_BODY": "no ticket here"},
    )

    assert r.returncode == 0
    assert calls == []
    assert "could not derive a task code" in r.stderr


def test_notify_batches_title_body_and_url_mergecommit_into_two_gh_calls(repo_with_pr_worktree, tmp_path):
    """The worst case (no $TASK_CODE reuse, code only in the PR body) must cost exactly TWO
    `gh pr view` round trips — one combined title+body query, one combined url+mergeCommit
    query — not four separate ones."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "refs HYP-931 for details"},
    )

    assert r.returncode == 0
    assert len(calls) == 1
    # Only the two NOTIFY calls are counted (title,body + url,mergeCommit) — the pre-existing
    # preflight/rollup `pr view` calls (headRefName, statusCheckRollup) are a separate, already
    # fixed cost this test doesn't care about and deliberately doesn't assert a total for
    # (that total would break on any unrelated preflight change).
    gh_calls = _gh_pr_view_calls(tmp_path)
    notify_calls = [c for c in gh_calls if "--json title,body" in c or "--json url,mergeCommit" in c]
    assert len(notify_calls) == 2, gh_calls


def test_notify_passes_the_local_repo_root_via_dash_c(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(main, bindir, tmp_path, branch="hyp-931-fix-thing")

    assert r.returncode == 0
    assert len(calls) == 1
    assert calls[0].split()[:2] == ["-C", str(main)]
