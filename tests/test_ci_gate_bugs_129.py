"""Tests for the agent-tools#129 CI-gate fixes — the SHIPPED catalog gate scripts/workflows
that rig copies verbatim into every consumer, so a bug here is a bug in every rigged repo.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ci_gate_bugs_129.py -q

Three classes of bug were fixed; each is asserted here against the REAL scripts/workflows:

1. dependency-review (SECURITY): the blocking gate ran the PR-checked-out script under plain
   `pull_request`, so a malicious PR could weaken the very gate it must pass. Fixed to the
   trusted-base `pull_request_target` model (mirrors leftover-grep / review-threads): the
   base-branch script runs; the PR's lockfiles are audited as DATA in a side worktree. We
   assert the workflow uses pull_request_target, runs the script from $GITHUB_WORKSPACE
   (the trusted base checkout), never `npm install`/`bun install`/checks out PR head onto the
   workspace, and that dep-audit.sh accepts an audit-dir arg and fails closed on a missing dir.
   The SECOND half of the bug: `setup-bun` was commented out, so a `bun.lock` repo fail-CLOSED
   (no `bun` on PATH -> dep-audit.sh's miss() reds CI). Fixed by shipping setup-bun ENABLED and
   SHA-pinned; we assert it is an active (uncommented), SHA-pinned step.

2. leftover-grep: a shallow head broke the three-dot merge-base diff and the script read the
   diff via a process substitution that swallowed `git diff` errors -> a block-tier gate could
   silently PASS having scanned nothing. Fixed to materialize the lines + check $? (fail
   closed), fail closed on an explicitly-requested base that doesn't resolve, and the workflow
   deepens the head fetch + verifies a merge-base. Also the `=======` conflict-marker false
   positive on a 7-`=` source separator is dropped (start/end markers still catch a real
   conflict). We exercise the real script for each.

3. codeql self-gate: the language-detect `git ls-files | grep -qiE` under `pipefail` returns
   141 (SIGPIPE) when grep matches early -> the `if` takes the else branch -> CodeQL silently
   SKIPS a language whose source IS present (the gate self-disables). Fixed to materialize the
   file list and grep a here-string with no -q pipe. We assert the buggy pattern is gone and
   reproduce that the new pattern detects.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# NOTE: `yaml` is imported per-test via importorskip (NOT at module level) so the many
# behavioral tests below — which exercise the real shipped shell scripts and need no YAML
# parser — still RUN in CI, where the dependency-free `uv run --with pytest` env has no
# PyYAML. Only the two structural tests that parse a workflow with yaml.safe_load skip when
# it's absent. (The repo's lib is stdlib-only at import time; PyYAML is a lazy/optional dep.)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEP_AUDIT = REPO_ROOT / "ci" / "dependency-review" / "dep-audit.sh"
DEP_WF = REPO_ROOT / "ci" / "dependency-review" / "workflow.yml"
LEFTOVER = REPO_ROOT / "ci" / "leftover-grep" / "leftover-grep.sh"
LEFTOVER_WF = REPO_ROOT / "ci" / "leftover-grep" / "workflow.yml"
CODEQL_WF = REPO_ROOT / "ci" / "codeql" / "workflow-selfgate.yml"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    # Bypass any machine-global hooks (require-ticket etc.) — these are throwaway fixtures.
    env = dict(os.environ, REQUIRE_TICKET_SKIP="1")
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", msg],
        cwd=repo, check=True, capture_output=True, text=True, env=env,
    )


def _run(script: Path, cwd: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    # Invoke via bash, matching how CI runs these scripts (leftover-grep.yml: `bash …`). The
    # scripts carry a `#!/usr/bin/env bash` shebang and use bash-only constructs
    # (`set -o pipefail`, `IFS=$'\t'`). Running them through `sh` would pass on macOS (where
    # /bin/sh is bash) but FAIL on the Ubuntu CI runner (where /bin/sh is dash) — a false
    # green locally that goes red in CI. dep-audit.sh is POSIX-clean and works under bash too.
    proc = subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 1. dependency-review — tamper-resistant trusted-base model.
# ---------------------------------------------------------------------------

def test_dependency_review_runs_under_pull_request_target():
    """The blocking gate must run under pull_request_target (base-trusted), not plain
    pull_request (where the PR can edit the gate script it has to pass)."""
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(DEP_WF.read_text())
    on = wf[True] if True in wf else wf["on"]  # PyYAML parses bare `on:` as boolean True
    assert "pull_request_target" in on, "dependency-review must use pull_request_target"
    assert "pull_request" not in on, "must NOT also trigger plain pull_request (PR-trusted)"


def test_dependency_review_runs_trusted_base_script_not_pr_copy():
    """The run: must invoke the script from $GITHUB_WORKSPACE — the trusted base checkout —
    so the PR's edited copy never runs."""
    text = DEP_WF.read_text()
    assert "$GITHUB_WORKSPACE/ci/dependency-review/dep-audit.sh" in text


def _executable_lines(text: str) -> str:
    """Lines that aren't YAML comments — so an assertion can't be fooled by a forbidden
    pattern quoted inside an explanatory `#` comment."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_dependency_review_never_executes_pr_code():
    """Hard rule: no install/build step and no checkout of the PR head onto the workspace
    under the privileged trigger — that would execute PR-controlled code. Asserted against
    executable (non-comment) lines so the documented prohibition doesn't trip the check."""
    code = _executable_lines(DEP_WF.read_text())
    assert "npm install" not in code
    assert "bun install" not in code
    assert "npm ci" not in code
    text = DEP_WF.read_text()
    # The PR head is fetched as a git object + side worktree, never checked out onto the tree.
    assert "git fetch --no-tags --depth=1 origin" in text
    assert "git worktree add --detach" in text


def test_dependency_review_installs_bun_toolchain():
    """The second half of agent-tools#129: a `bun.lock` repo fail-CLOSED because `setup-bun`
    was commented out (dep-audit.sh found the lockfile, couldn't find `bun`, and red'd CI).
    The toolchain must now ship ENABLED (an active `uses:` step, not a comment) and SHA-pinned
    so the audit actually RUNS instead of fail-closing."""
    uses = [
        ln.strip()
        for ln in DEP_WF.read_text().splitlines()
        if not ln.lstrip().startswith("#")
        and ln.lstrip().startswith("uses:")
        and "setup-bun" in ln
    ]
    assert uses, "setup-bun must be an active (uncommented) `uses:` step, not a comment example"
    for ln in uses:
        assert re.search(r"@[0-9a-f]{40}\b", ln), f"setup-bun must be SHA-pinned: {ln!r}"


def test_dep_audit_fails_closed_on_bun_lock_without_bun(tmp_path: Path):
    """Proves WHY setup-bun must be installed: a bun.lock tree with no `bun` on PATH fails
    CLOSED (rc 1) — exactly the red CI the toolchain install removes. DEP_AUDIT_ALLOW_MISSING=1
    is the only intentional escape."""
    tree = tmp_path / "bunrepo"
    tree.mkdir()
    (tree / "bun.lock").write_text("# lockfile\n")
    env = dict(os.environ, PATH="/usr/bin:/bin")  # no bun resolvable
    proc = subprocess.run(
        ["bash", str(DEP_AUDIT), str(tree)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bun" in (proc.stdout + proc.stderr).lower()
    proc_open = subprocess.run(
        ["bash", str(DEP_AUDIT), str(tree)],
        env=dict(env, DEP_AUDIT_ALLOW_MISSING="1"), capture_output=True, text=True, timeout=60,
    )
    assert proc_open.returncode == 0, proc_open.stdout + proc_open.stderr


def test_dep_audit_accepts_audit_dir_and_fails_closed_on_missing(tmp_path: Path):
    """dep-audit.sh takes the audit dir as $1 and fails CLOSED if it doesn't exist — a
    vanished audit target must not masquerade as 'no manifests, nothing to audit'."""
    rc, out = _run(DEP_AUDIT, tmp_path, str(tmp_path / "does-not-exist"))
    assert rc == 1, out
    assert "does not exist" in out


def test_dep_audit_empty_tree_passes(tmp_path: Path):
    """No supported manifest in the audited dir -> nothing to audit, clean pass (rc 0)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, out = _run(DEP_AUDIT, tmp_path, str(empty))
    assert rc == 0, out
    assert "nothing to audit" in out


def test_dep_audit_pip_audit_is_not_resolving():
    """SECURITY (agent-tools#129): under pull_request_target dep-audit.sh runs against the PR
    tree as DATA. pip-audit is the one auditor that can execute input — a resolving run
    (`-r`/`-e`/`--requirement` without `--no-deps`) builds the PR's sdists (runs setup.py) ->
    arbitrary PR code under a privileged trigger (RCE). Pin that the invocation never gains a
    resolving flag without `--no-deps`, so a future edit can't silently reopen the hole."""
    code = _executable_lines(DEP_AUDIT.read_text())
    m = re.search(r"pip-audit\b[^\n|;]*(?:\s-r\b|\s-e\b|--requirement\b)", code)
    if m:
        assert "--no-deps" in m.group(0), (
            f"resolving pip-audit without --no-deps (RCE under pull_request_target): {m.group(0)!r}"
        )


# ---------------------------------------------------------------------------
# 2. leftover-grep — fail closed on a missing/unresolvable base; no swallowed diff error.
# ---------------------------------------------------------------------------

def _leftover_repo(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("a = 1\n")
    _commit(repo, "base")
    _git(repo, "branch", "-M", "main")
    return repo


def test_leftover_fails_closed_on_unresolvable_explicit_base(tmp_path: Path):
    """An explicitly-requested base that doesn't resolve must FAIL the gate, not silently
    fall back to a full-tree scan (flood) or a no-op."""
    repo = _leftover_repo(tmp_path)
    rc, out = _run(LEFTOVER, repo, env_extra={
        "LEFTOVER_BASE": "origin/does-not-exist", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "does not resolve" in out


def test_leftover_fails_closed_on_no_merge_base(tmp_path: Path):
    """The #129 core bug: a head with no merge-base vs the base makes `git diff base...HEAD`
    error. That error must FAIL the gate (it used to be swallowed by `done < <(emit_lines)`,
    so the gate printed PASS having scanned nothing)."""
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("a = 1\n")
    _commit(repo, "a")
    _git(repo, "branch", "-M", "main")
    # Orphan branch = unrelated history = no merge-base.
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "rm", "-q", "-rf", ".")
    (repo / "b.py").write_text("b = 2\n")
    _commit(repo, "orphan")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "could not compute the lines to scan" in out


def test_leftover_empty_diff_passes(tmp_path: Path):
    """No added lines at all (head == base) -> emit_lines yields nothing and returns 0. The
    new `if ! emit_lines >file` invariant must PASS cleanly, not false-fail with 'could not
    compute the lines to scan' on the legitimate empty-diff path (review finding #2)."""
    repo = _leftover_repo(tmp_path)
    # A branch identical to main: a real, resolvable base + head, but an empty added-lines diff.
    _git(repo, "checkout", "-q", "-b", "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out
    assert "PASS" in out


def test_leftover_untracked_todo_blocks(tmp_path: Path):
    """Sanity: a real leftover (untracked TODO on an added line) still blocks."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("a = 1\nb = 2  # TODO no ticket\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "untracked-todo" in out


def test_leftover_tracked_todo_passes(tmp_path: Path):
    """A TODO WITH a tracker ref passes."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("a = 1\nb = 2  # TODO(#42) tracked\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out


def test_leftover_bare_seven_equals_is_not_a_conflict_marker(tmp_path: Path):
    """A source line of exactly 7 `=` is a common decorative separator, not a merge marker —
    it must NOT block (agent-tools#129 false positive)."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "sep.py").write_text("x = 1\n=======\ny = 2\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out
    assert "merge-marker" not in out


def test_leftover_real_conflict_start_marker_blocks(tmp_path: Path):
    """A genuine `<<<<<<<` start marker still blocks — the conflict is caught by its
    unambiguous start/end markers even though the bare `=======` middle is no longer flagged."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "c.py").write_text("<<<<<<< HEAD\nx = 1\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "merge-marker" in out


def test_leftover_script_does_not_read_emit_lines_via_process_substitution():
    """Guard the fix: the script must NOT pipe emit_lines through `< <(...)` (which swallows
    its failure). It writes to a temp file and gates on the exit status."""
    code = "\n".join(
        ln for ln in LEFTOVER.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "done < <(emit_lines)" not in code
    assert "if ! emit_lines >" in code


# ---------------------------------------------------------------------------
# 3. codeql self-gate — language-detect must not self-disable via SIGPIPE.
# ---------------------------------------------------------------------------

def test_codeql_detect_does_not_use_pipe_grep_q():
    """The buggy `git ls-files | grep -qiE` (dies of SIGPIPE under pipefail -> false negative)
    must be gone; the detect materializes the list and reads grep's exit code explicitly."""
    code = "\n".join(
        ln for ln in CODEQL_WF.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "git ls-files | grep -qiE" not in code
    assert 'tracked="$(git ls-files)"' in code
    assert 'grep -qiE "$pattern" <<<"$tracked"' in code


def test_codeql_detect_pattern_detects_present_source_under_pipefail():
    """Reproduce the fix end-to-end: under `set -uo pipefail`, the new detect pattern must
    report DETECTED when many matching files exist (the old pipe pattern reported MISSED
    because git ls-files died of SIGPIPE when grep -q exited early)."""
    script = r'''
    set -uo pipefail
    tracked=$(seq 1 100000 | sed "s/.*/file&.ts/")
    match_rc=0
    grep -qiE "\.ts$" <<<"$tracked" || match_rc=$?
    if [ "$match_rc" -gt 1 ]; then echo ERROR
    elif [ "$match_rc" -eq 0 ]; then echo DETECTED
    else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "DETECTED", proc.stdout + proc.stderr


def test_codeql_old_pipe_pattern_self_disables_proving_the_bug():
    """Pin the regression: the OLD `printf|grep -q` pipe pattern DOES self-disable (reports
    MISSED) under pipefail — proving the fix above is load-bearing, not cosmetic."""
    script = r'''
    set -uo pipefail
    big=$(seq 1 100000 | sed "s/.*/file&.ts/")
    if printf "%s\n" "$big" | grep -qiE "\.ts$"; then echo DETECTED; else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "MISSED", proc.stdout + proc.stderr


def test_codeql_doc_matches_block_walk_impl():
    """Doc/code drift fix: the header must describe the contiguous-comment-block suppression
    walk (what the impl does), not 'the line directly above'."""
    text = CODEQL_WF.read_text()
    # The header now says "ANY line in the contiguous comment block".
    assert "contiguous comment block" in text
    # The impl still walks the block.
    assert "Walk upward through the contiguous comment block" in text


def test_all_touched_workflows_parse():
    """Every workflow YAML touched here must still parse."""
    yaml = pytest.importorskip("yaml")
    for wf in (DEP_WF, LEFTOVER_WF, CODEQL_WF):
        yaml.safe_load(wf.read_text())
