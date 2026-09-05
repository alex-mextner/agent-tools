"""Tests for ci/ship/ship.sh — the merge-time version auto-bump (#518).

The version-bump gate used to make every parallel PR bump the same line to the same next
version, so the second one to merge was CONFLICTING. Now, when that gate would refuse, ship
bumps the PATCH version itself on the PR head branch through the GitHub Contents API (after
updating the branch from base when base's version already moved), then carries on through the
pipeline treating its own commit correctly at every gate.

Hermetic, but REAL where it matters: the fake `gh` below is backed by a real bare `origin` +
a private clone, so `pr diff`, the Contents API GET/PUT, `update-branch` and `pr merge` are
real git operations (a squash merge that would conflict really fails). ship.sh's own local
half (branch sanity, fast-forward, cleanup) runs against a separate clone of the same origin.
The graphql fakes run the REAL `--jq` filter ship passes over a canned payload, so the dwell
and thread filters are exercised, not stubbed.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_auto_bump.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

_OLD_DATE = "2020-01-01T00:00:00Z"

_FAKE_GH = r'''#!/usr/bin/env bash
# Fake gh for the auto-bump suite: state lives in $SHIP_TEST_STATE (a clone of the bare origin
# under clone/, threads.json, gh.log). See the module docstring of the test file.
set -e
S="$SHIP_TEST_STATE"; C="$S/clone"; LOG="$S/gh.log"; B="$SHIP_TEST_BRANCH"
argstr="$*"
log() { printf '%s\n' "$*" >> "$LOG"; }
git_c() { git -C "$C" -c core.hooksPath= -c user.email=gh@fake -c user.name=fakegh "$@"; }
green='[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
red='[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"FAILURE","workflowName":"CI"}]'
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if grep -q -- '--json reviews' <<<"$argstr"; then echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif grep -q -- '--json mergeStateStatus' <<<"$argstr"; then echo "${SHIP_TEST_MERGE_STATE_AFTER:-CLEAN}"
        elif grep -q headRefName <<<"$argstr"; then printf '%s\tOPEN\tMERGEABLE\t%s\t%s\n' "$B" "${SHIP_TEST_CROSS_REPO:-false}" "${SHIP_TEST_MERGE_STATE:-CLEAN}"
        elif grep -q statusCheckRollup <<<"$argstr"; then
          git_c fetch -q origin
          msg=$(git_c log -1 --format=%s "origin/$B")
          if [ -n "${SHIP_TEST_RED_AFTER_BUMP:-}" ] && grep -q 'ship auto-bump' <<<"$msg"; then echo "$red"; else echo "$green"; fi
        elif grep -q headRefOid <<<"$argstr"; then git_c fetch -q origin; git_c rev-parse "origin/$B"
        elif grep -q baseRefName <<<"$argstr"; then echo main
        elif grep -q -- '--json commits' <<<"$argstr"; then git_c fetch -q origin; git_c log -1 --format=%s "origin/$B"
        else echo '[]'; fi ;;
      diff)
        git_c fetch -q origin
        if grep -q -- '--name-only' <<<"$argstr"; then git_c diff --name-only "origin/main...origin/$B"
        else git_c diff "origin/main...origin/$B"; fi ;;
      merge)
        log "merge $*"
        git_c fetch -q origin
        git_c checkout -q -B main origin/main
        if ! git_c merge --squash -q "origin/$B" >/dev/null 2>&1; then
          echo "fake gh: squash merge CONFLICT" >&2; git_c reset -q --hard; exit 1
        fi
        git_c commit -q -m "squash PR ($B)"
        git_c push -q origin main
        echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    method=GET; endpoint=""; jqexpr=""
    F_message=""; F_content=""; F_sha=""; F_branch=""; F_expected=""; F_query=""; F_id=""
    F_committer_email=""; F_committer_name=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -X) method="$2"; shift 2 ;;
        -f|-F) kv="$2"; shift 2; k="${kv%%=*}"; v="${kv#*=}"
          case "$k" in
            message) F_message="$v" ;; content) F_content="$v" ;; sha) F_sha="$v" ;; branch) F_branch="$v" ;;
            expected_head_sha) F_expected="$v" ;; query) F_query="$v" ;; id) F_id="$v" ;;
            committer\[email\]) F_committer_email="$v" ;; committer\[name\]) F_committer_name="$v" ;;
          esac ;;
        --jq) jqexpr="$2"; shift 2 ;;
        -*) shift ;;
        *) endpoint="$1"; shift ;;
      esac
    done
    case "$endpoint" in
      graphql)
        if grep -q resolveReviewThread <<<"$F_query"; then
          log "resolve $F_id"
          tmp=$(jq --arg id "$F_id" '(.data.repository.pullRequest.reviewThreads.nodes[] | select(.id==$id) | .isResolved) = true' "$S/threads.json")
          printf '%s' "$tmp" > "$S/threads.json"; echo '{"data":{}}'
        elif grep -q reviewThreads <<<"$F_query"; then
          if [ -f "$S/threads.json" ]; then jq -r "$jqexpr" "$S/threads.json"
          else printf '%s' '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}}}' | jq -r "$jqexpr"; fi
        elif grep -q committedDate <<<"$F_query"; then
          git_c fetch -q origin
          # A PR's commit list = commits reachable from head but not from base (a merge FROM base
          # is in it; base's own commits are not), chronological; ship asks for the last 15. Build
          # each node's JSON via jq (one commit at a time) rather than a hand-rolled control-byte
          # field separator in the git-log format string -- a literal separator byte embedded in a
          # bash script is invisible and fragile (a real review finding on this file, #518).
          nodes='[]'
          for _sha in $(git_c log --reverse --format=%H "origin/main..origin/$B" | tail -15); do
            _msg=$(git_c log -1 --format=%s "$_sha")
            _cdate=$(TZ=UTC git_c log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd "$_sha")
            _cemail=$(git_c log -1 --format=%ce "$_sha")
            nodes=$(jq --arg msg "$_msg" --arg cd "$_cdate" --arg ce "$_cemail" \
              '. + [{commit:{message:$msg, committedDate:$cd, pushedDate:null, committer:{email:$ce}}}]' <<<"$nodes")
          done
          jq -n --argjson nodes "$nodes" --arg created "${SHIP_TEST_CREATED:-2020-01-01T00:00:00Z}" \
            '{data:{repository:{pullRequest:{createdAt:$created, commits:{nodes:$nodes}, timelineItems:{nodes:[]}}}}}' | jq -r "$jqexpr"
        else echo 0; fi ;;
      repos/*/contents/*)
        path="${endpoint#*/contents/}"; file="${path%%\?*}"; ref="${path#*ref=}"; [ "$ref" = "$path" ] && ref=""
        git_c fetch -q origin
        if [ "$method" = "GET" ]; then
          log "GET contents file=$file ref=$ref"
          blob=$(git_c rev-parse "origin/$ref:$file" 2>/dev/null) || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
          content=$(git_c show "origin/$ref:$file" | base64 | tr -d '\n')
          jq -n --arg sha "$blob" --arg content "$content" '{sha:$sha, content:($content+"\n"), encoding:"base64"}' | jq -r "$jqexpr"
        else
          log "PUT contents file=$file branch=$F_branch sha=$F_sha message=$F_message"
          [ -n "${SHIP_TEST_BUMP_PUT_FAIL:-}" ] && { echo "gh: Update is not a fast forward (HTTP 409)" >&2; exit 1; }
          cur=$(git_c rev-parse "origin/$F_branch:$file")
          [ "$cur" = "$F_sha" ] || { echo "gh: $file does not match $F_sha (HTTP 409)" >&2; exit 1; }
          git_c checkout -q -B "$F_branch" "origin/$F_branch"
          printf '%s' "$F_content" | base64 -d > "$C/$file"
          git_c add -- "$file"
          # Real GitHub Contents API honours an explicit author/committer object -- ship.sh
          # (#518) always sends one so the review-dwell gate can recognize its own commit by
          # COMMITTER IDENTITY rather than by message text (see SHIP_AUTO_BUMP_COMMITTER_EMAIL
          # in ship.sh). GIT_COMMITTER_* env wins over the `-c user.email=` baked into git_c().
          GIT_AUTHOR_NAME="${F_committer_name:-t}" GIT_AUTHOR_EMAIL="${F_committer_email:-t@t}" \
          GIT_COMMITTER_NAME="${F_committer_name:-t}" GIT_COMMITTER_EMAIL="${F_committer_email:-t@t}" \
          git_c commit -q -m "$F_message"
          git_c push -q origin "$F_branch"
          jq -n --arg sha "$(git_c rev-parse HEAD)" '{commit:{sha:$sha}}' | jq -r "$jqexpr"
        fi ;;
      repos/*/pulls/*/update-branch)
        log "PUT update-branch expected=$F_expected"
        git_c fetch -q origin
        git_c checkout -q -B "$B" "origin/$B"
        if ! git_c merge -q --no-ff -m "Merge branch 'main' into $B" origin/main >/dev/null 2>&1; then
          git_c merge --abort 2>/dev/null || true; echo "gh: merge conflict (HTTP 422)" >&2; exit 1
        fi
        git_c push -q origin "$B"
        echo '{"message":"Updating pull request branch.","url":""}' ;;
      *) echo 0 ;;
    esac ;;
  *) : ;;
esac
'''

_PYPROJECT = '[project]\nname = "mytool"\nversion = "1.0.0"\n'
_VERSION_LINE = 3  # 1-based line of `version = ...` in _PYPROJECT


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd, date=_OLD_DATE):
    """Bootstrap git op with hooks neutralized and an OLD commit date (so the review-dwell gate
    is satisfied by the PR's own commits and only ship's fresh commits are 'now')."""
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    r = _sh("git", "-c", "core.hooksPath=", "-c", "user.email=t@t", "-c", "user.name=t", *args, cwd=cwd, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def _commit_file(repo: Path, rel: str, text: str, msg: str, date=_OLD_DATE):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo, date=date)
    _git("commit", "-qm", msg, cwd=repo, date=date)


def _make_world(tmp_path: Path, *, with_version_file: bool = True):
    """A bare origin, ship's local clone `main` (branch main), and the fake gh's state dir."""
    if not shutil.which("bash") or not shutil.which("git") or not shutil.which("jq"):
        pytest.skip("bash/git/jq required")
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    if with_version_file:
        (main / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    state = tmp_path / "state"
    state.mkdir()
    _sh("git", "clone", "-q", str(origin), str(state / "clone"), cwd=tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return main, origin, state, bindir


def _make_pr_branch(main: Path, branch: str, rel: str, text: str, *, date=_OLD_DATE) -> Path:
    """Branch off main's current tip in a worktree, commit one file, push. Returns the worktree."""
    wt = main.parent / f"wt-{branch}"
    _git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=main)
    _commit_file(wt, rel, text, f"feat: {rel}", date=date)
    _git("push", "-q", "origin", branch, cwd=wt)
    return wt


def _run_ship(main: Path, bindir: Path, state: Path, branch: str, *extra, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_STATE"] = str(state)
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "0"
    env["SHIP_TASK_NOTIFY_ENABLED"] = "0"
    env["SHIP_AUDIT_FILE"] = str(state / "audit.jsonl")
    env["SHIP_AUTO_BUMP_HEAD_WAIT"] = "5"
    env["SHIP_AUTO_BUMP_HEAD_POLL"] = "1"
    env.pop("SHIP_AUTO_BUMP", None)  # the feature under test defaults ON
    if env_extra:
        env.update(env_extra)
    return _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra, cwd=cwd or main, env=env)


def _origin_file(origin: Path, ref: str, rel: str) -> str:
    r = _sh("git", "show", f"{ref}:{rel}", cwd=origin)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _origin_log(origin: Path, ref: str) -> list[str]:
    r = _sh("git", "log", "--format=%s", ref, cwd=origin)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def _gh_log(state: Path) -> str:
    p = state / "gh.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _thread(tid: str, *, path: str, line: int, login: str, resolved: bool = False) -> dict:
    return {
        "id": tid, "isResolved": resolved, "path": path, "line": line, "originalLine": line,
        "comments": {"totalCount": 1, "nodes": [{"author": {"login": login}, "body": "nit: version"}]},
    }


def _write_threads(state: Path, *threads: dict) -> None:
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(threads)}}}}}
    (state / "threads.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------------------
# The core: a source PR with no bump is auto-bumped and merged.
# ---------------------------------------------------------------------------------------

def test_source_pr_without_bump_is_auto_bumped_and_merged(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump: pyproject.toml 1.0.0 -> 1.0.1 committed as" in r.stdout, r.stdout
    assert "version-bump gate OK — pyproject.toml version auto-bumped 1.0.0 -> 1.0.1" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout
    log = _gh_log(state)
    assert "PUT contents file=pyproject.toml branch=feat" in log, log
    assert "chore(release): bump version 1.0.0 -> 1.0.1 (ship auto-bump for #1)" in log, log
    assert "PUT update-branch" not in log, "base had not moved: no branch update expected"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")
    # The bump commit went onto the PR HEAD branch, not main directly.
    assert "PUT contents file=pyproject.toml branch=feat" in log
    # The worktree was fast-forwarded to the bumped head before the (second) sanity check.
    assert "fast-forwarded worktree" in r.stdout, r.stdout
    assert not wt.exists(), "cleanup should have removed the PR worktree"


def test_back_to_back_prs_neither_touching_version_file_both_merge(tmp_path):
    """The acceptance criterion of #518: two PRs branched from the same main, neither touching
    pyproject.toml. The first is auto-bumped 1.0.0 -> 1.0.1. The second's merge-base still
    reads 1.0.0 while main reads 1.0.1 — a naive bump on its head (1.0.0 -> 1.0.2) would
    CONFLICT in the squash three-way merge on the version line. ship updates the branch from
    main first, then bumps 1.0.1 -> 1.0.2, and the real squash merge succeeds."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat-a", "src/a.py", "a = 1\n")
    _make_pr_branch(main, "feat-b", "src/b.py", "b = 1\n")

    ra = _run_ship(main, bindir, state, "feat-a")
    assert ra.returncode == 0, f"{ra.stdout}\n{ra.stderr}"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")

    rb = _run_ship(main, bindir, state, "feat-b")
    assert rb.returncode == 0, f"{rb.stdout}\n{rb.stderr}"
    assert "updating feat-b from main first" in rb.stdout, rb.stdout
    assert "auto-bump: pyproject.toml 1.0.1 -> 1.0.2 committed as" in rb.stdout, rb.stdout
    assert "[fake gh] merged" in rb.stdout
    log = _gh_log(state)
    assert "PUT update-branch" in log, log
    assert 'version = "1.0.2"' in _origin_file(origin, "main", "pyproject.toml")
    # Both PRs' files are on main: neither merge was lost to a conflict.
    assert _origin_file(origin, "main", "src/a.py") == "a = 1\n"
    assert _origin_file(origin, "main", "src/b.py") == "b = 1\n"


def test_api_push_rejected_refuses_and_merges_nothing(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_BUMP_PUT_FAIL": "1"})

    assert r.returncode != 0, r.stdout
    assert "version-bump commit was REJECTED by GitHub" in r.stderr, r.stderr
    assert "HTTP 409" in r.stderr, r.stderr
    assert "Nothing was merged" in r.stderr
    assert "merge" not in _gh_log(state).replace("PUT", ""), _gh_log(state)
    assert "[fake gh] merged" not in r.stdout
    assert 'version = "1.0.0"' in _origin_file(origin, "feat", "pyproject.toml"), "branch must be untouched"


def test_ci_red_after_bump_refuses_with_guidance_and_rerun_needs_no_second_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_RED_AFTER_BUMP": "1"})

    assert r.returncode != 0, r.stdout
    assert "PUT contents file=pyproject.toml branch=feat" in _gh_log(state)
    assert "waiting on the checks of ship's own bump commit" in r.stdout, r.stdout
    assert "check(s) not passing" in r.stderr, r.stderr
    assert "Fix CI, then re-run" in r.stderr
    assert "[fake gh] merged" not in r.stdout
    # The bump commit is left on the branch (the refusal message points at fixing CI, not at
    # the bump), so a re-run sees the PR as already bumped and makes NO second bump.
    assert _origin_log(origin, "feat")[0].startswith("chore(release): bump version 1.0.0 -> 1.0.1")
    put_count_before = _gh_log(state).count("PUT contents")

    r2 = _run_ship(main, bindir, state, "feat")

    assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr}"
    assert "PR already bumps pyproject.toml (1.0.0 -> 1.0.1" in r2.stdout, r2.stdout
    assert _gh_log(state).count("PUT contents") == put_count_before, "no second bump on re-run"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")


def test_pr_that_already_bumps_gets_no_second_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.1.0"), "chore: minor bump")
    _git("push", "-q", "origin", "feat", cwd=wt)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "not needed — PR already bumps pyproject.toml (1.0.0 -> 1.1.0, main at 1.0.0)" in r.stdout, r.stdout
    assert "version-bump gate OK — pyproject.toml version is bumped in PR #1" in r.stdout, r.stdout
    assert "PUT contents" not in _gh_log(state)
    assert 'version = "1.1.0"' in _origin_file(origin, "main", "pyproject.toml")


def test_hand_bump_raced_by_main_is_rebumped_on_top_of_base(tmp_path):
    """The parallel-PR race the issue describes: the PR hand-bumped to 1.0.1, but main already
    shipped 1.0.1 (another PR). Squashing as-is would conflict, and merging clean would land
    NO bump. ship updates the branch (the two 1.0.1 lines merge clean) and bumps 1.0.1 -> 1.0.2."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore: patch bump")
    _git("push", "-q", "origin", "feat", cwd=wt)
    # main moves to 1.0.1 independently (another PR shipped).
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "bumps pyproject.toml 1.0.0 -> 1.0.1 but main is already at 1.0.1 — re-bumping" in r.stdout, r.stdout
    assert "PUT update-branch" in _gh_log(state)
    assert 'version = "1.0.2"' in _origin_file(origin, "main", "pyproject.toml")


def test_opt_out_via_env_restores_the_refusal(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_AUTO_BUMP": "0"})

    assert r.returncode != 0, r.stdout
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "did not here because: SHIP_AUTO_BUMP=0" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)
    assert "[fake gh] merged" not in r.stdout


def test_opt_out_via_committed_ship_config(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _commit_file(main, ".ship-config", "SHIP_AUTO_BUMP=0\n", "chore: opt out of auto-bump")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "SHIP_AUTO_BUMP=0 (env or .ship-config)" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)


def test_ship_config_opt_out_must_be_committed_at_head(tmp_path):
    """An uncommitted .ship-config is ignored (audited-config rule), so the auto-bump stays on."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    (main / ".ship-config").write_text("SHIP_AUTO_BUMP=0\n", encoding="utf-8")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PUT contents" in _gh_log(state)
    (main / ".ship-config").unlink()


def test_repo_without_version_file_keeps_note_and_skip(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "no version file (pyproject.toml/package.json) found at repo root — skipping" in r.stdout, r.stdout
    assert "PUT contents" not in _gh_log(state)
    assert "[fake gh] merged" in r.stdout


def test_dry_run_prints_the_plan_and_pushes_nothing(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", "--dry-run")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "[dry-run] would bump pyproject.toml 1.0.0 -> 1.0.1 on feat via the GitHub Contents API" in r.stdout, r.stdout
    assert "nothing pushed" in r.stdout
    assert "[dry-run] version-bump gate would be satisfied by the planned auto-bump" in r.stdout, r.stdout
    assert "would append version-bump audit: decision=version-bump:auto" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)
    assert "merge" not in _gh_log(state)
    assert not (state / "audit.jsonl").exists()
    assert _origin_log(origin, "feat")[0] == "feat: src/a.py"


def test_dry_run_plans_the_branch_update_when_base_moved(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", "--dry-run")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "[dry-run] would update feat from main first (main is at 1.0.1, this PR's merge-base at 1.0.0)" in r.stdout, r.stdout
    assert "[dry-run] would bump pyproject.toml 1.0.1 -> 1.0.2" in r.stdout, r.stdout
    assert "PUT" not in _gh_log(state)


def test_review_dwell_is_measured_from_the_last_non_ship_push(tmp_path):
    """The PR's own commits are old (2020); ship's bump commit is 'now'. With the default
    600s window the merge passes only because the dwell query skips ship's trailing bump."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "review-dwell gate OK" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout


def test_review_dwell_still_counts_a_fresh_human_push(tmp_path):
    """Negative control for the test above: a fresh (now) commit by the PR author — not ship —
    is a real push and the dwell gate refuses on it (the skip is narrow, not a blanket bypass)."""
    import datetime as _dt
    main, origin, state, bindir = _make_world(tmp_path)
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n", date=now)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode != 0, r.stdout
    assert "review-dwell window is 600s" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_dwell_skips_ship_update_branch_merge_before_the_bump(tmp_path):
    """When base moved, ship's update-branch merge commit (also 'now') sits right before the
    bump; both are ship's and neither resets the window."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "src/other.py", "o = 1\n", "feat: other")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PUT update-branch" in _gh_log(state)
    assert "review-dwell gate OK" in r.stdout, r.stdout


def test_bot_thread_on_the_bump_line_is_auto_resolved(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_BOT", path="pyproject.toml", line=_VERSION_LINE, login="chatgpt-codex-connector"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump threads: resolved bot thread T_BOT on ship's bump line pyproject.toml:3" in r.stdout, r.stdout
    assert "resolve T_BOT" in _gh_log(state)
    assert "[fake gh] merged" in r.stdout


def test_human_thread_on_the_bump_line_still_blocks(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_HUMAN", path="pyproject.toml", line=_VERSION_LINE, login="alex"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_HUMAN" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_bot_thread_elsewhere_in_the_version_file_is_not_touched(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_OTHER", path="pyproject.toml", line=1, login="chatgpt-codex-connector"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_OTHER" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr


def test_audit_line_records_the_auto_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    lines = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    bump = [l for l in lines if l.get("decision") == "version-bump:auto"]
    assert len(bump) == 1, lines
    rec = bump[0]
    assert rec["pr"] == "1" and rec["gate"] == "version-bump" and rec["file"] == "pyproject.toml"
    assert rec["old"] == "1.0.0" and rec["new"] == "1.0.1" and rec["branch"] == "feat"
    assert len(rec["sha"]) == 40 and all(c in "0123456789abcdef" for c in rec["sha"]), rec
    # The recorded sha is the real bump commit ship authored on the PR branch (it survives in
    # the origin object store even after the squash + branch delete).
    assert _sh("git", "cat-file", "-t", rec["sha"], cwd=origin).stdout.strip() == "commit"
    subject = _sh("git", "log", "-1", "--format=%s", rec["sha"], cwd=origin).stdout.strip()
    assert subject == "chore(release): bump version 1.0.0 -> 1.0.1 (ship auto-bump for #1)", subject


def test_diverged_local_branch_refuses_before_anything_is_pushed(tmp_path):
    """The branch-sanity check runs BEFORE the bump: a local commit that was never pushed must
    refuse the ship with nothing written to the remote (ship would otherwise diverge it further)."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "src/b.py", "b = 1\n", "feat: unpushed")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "unpushed commit(s)" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)


def test_package_json_is_bumped_too(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _commit_file(main, "package.json", '{\n  "name": "mytool",\n  "version": "2.3.9"\n}\n', "chore: package.json")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.js", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump: package.json 2.3.9 -> 2.3.10 committed as" in r.stdout, r.stdout
    assert '"version": "2.3.10"' in _origin_file(origin, "main", "package.json")
    assert _origin_file(origin, "main", "package.json").endswith("}\n"), "byte layout preserved"


# ---------------------------------------------------------------------------------------
# Generic BEHIND-clearing (#518): mergeStateStatus=BEHIND fires whenever ANY prior merge
# moved base ahead under a require-up-to-date-branches ruleset, independent of whether the
# version file itself diverged — the acceptance criterion's own headline scenario ("two PRs
# shipped back to back both merge without a conflict") would otherwise still need a human
# `gh pr update-branch` the instant the first of the two merges, on such a repo.
# ---------------------------------------------------------------------------------------

def test_behind_pr_is_updated_and_merges_without_a_manual_rebase(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    # main must genuinely advance past feat's merge-base -- otherwise update-branch's merge is
    # a no-op ("already up to date", no new commit) and the head-oid wait below never sees a
    # change, regardless of what mergeStateStatus the fake claims.
    _commit_file(main, "src/other.py", "o = 1\n", "feat: unrelated change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_TEST_MERGE_STATE_AFTER": "CLEAN",
    })

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PR #1 is BEHIND its base — attempting to update it via the GitHub API" in r.stderr, r.stderr
    assert "no longer BEHIND" in r.stderr, r.stderr
    assert "PUT update-branch" in _gh_log(state)
    assert "[fake gh] merged" in r.stdout
    assert "head is BEHIND its base. Update it" not in r.stderr


def test_behind_pr_with_a_real_conflict_still_refuses(tmp_path):
    """The update-branch attempt can genuinely conflict (base and the branch both edited the
    same file differently) -- that still refuses with the original guidance, nothing merged."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "src/a.py", "x = 2\n", "feat: change a.py differently")
    _git("push", "-q", "-f", "origin", "feat", cwd=wt)
    _commit_file(main, "src/a.py", "x = 3\n", "chore: conflicting change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" in r.stderr, r.stderr
    assert "falling through to the BEHIND refusal" in r.stderr, r.stderr
    assert "head is BEHIND its base. Update it (gh pr update-branch" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_behind_resolution_skipped_when_auto_bump_opted_out(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_AUTO_BUMP": "0",
    })

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" not in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state)
    assert "head is BEHIND its base. Update it" in r.stderr, r.stderr


def test_behind_resolution_skipped_under_dry_run(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", "--dry-run", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" not in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state)
    assert "head is BEHIND its base. Update it" in r.stderr, r.stderr


def test_fork_pr_is_never_written_to(tmp_path):
    """A fork PR's head lives in the fork: ship must not (and could not) commit to it."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_CROSS_REPO": "true"})

    assert r.returncode != 0, r.stdout
    assert "PR head lives in a fork — ship cannot commit to it" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)


def test_non_semver_version_is_left_to_a_human(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.0rc1"), "chore: rc")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "is not a plain X.Y.Z — bump it by hand" in r.stderr, r.stderr
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)
