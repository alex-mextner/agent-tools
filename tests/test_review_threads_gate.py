"""Tests for the review-threads merge gate — agent-tools#65 (stale-green re-evaluation).

The shipped `ci/review-threads/workflow.yml` is copied verbatim by rig into every consumer,
so a trigger/tamper-resistance bug here is a bug in every rigged repo's required check.

#65: the gate only fired on PR-lifecycle events (opened/synchronize/...), so a review thread
ADDED AFTER an earlier green check left the required `review-threads` status DISHONESTLY green
— a PR could be merged via the GitHub UI over a fresh unresolved thread. The fix re-runs the
gate when a review/review-comment appears (the moment an unresolved thread is created) while
keeping the `pull_request_target` tamper-resistance intact.

These assertions pin:
  • the re-evaluation triggers (`pull_request_review`, `pull_request_review_comment`) exist,
  • the deliberately-EXCLUDED events stay out (each is a false-coverage no-op — see below),
  • the checkout pins `ref` to the trusted PR BASE branch (the review/comment triggers default
    to the PR MERGE ref, which contains PR-head edits to the gate script),
  • PR-number resolution covers every trigger that reaches the job.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_review_threads_gate.py -q
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

# `yaml` is imported per-test via importorskip (NOT at module level) so the string/structural
# checks below still RUN where the dependency-free `uv run --with pytest` env lacks PyYAML;
# only the safe_load parse-tests skip when it's absent.
REPO_ROOT = Path(__file__).resolve().parent.parent
RT_WF = REPO_ROOT / "ci" / "review-threads" / "workflow.yml"
RT_SH = REPO_ROOT / "ci" / "review-threads" / "review-threads.sh"
RT_README = REPO_ROOT / "ci" / "review-threads" / "README.md"


def _on_block(wf: dict) -> dict:
    """PyYAML parses a bare `on:` key as the boolean True; tolerate either spelling."""
    return wf[True] if True in wf else wf["on"]


def _executable_lines(text: str) -> str:
    """Non-comment lines, so an assertion can't be fooled by a pattern quoted in a `#` comment."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------------------
# Triggers: the re-evaluation events that close the stale-green gap (#65).
# ---------------------------------------------------------------------------

def test_workflow_parses():
    yaml = pytest.importorskip("yaml")
    yaml.safe_load(RT_WF.read_text())


def test_keeps_pull_request_lifecycle_trigger():
    """Don't break the original path: the lifecycle gate (open/synchronize/...) stays."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_target" in on, "must keep the tamper-resistant lifecycle trigger"
    types = on["pull_request_target"]["types"]
    for t in ("opened", "synchronize", "reopened"):
        assert t in types, f"pull_request_target must still fire on {t}"


def test_reruns_when_a_review_is_submitted():
    """A submitted review is when review threads get added — the gate must re-count then."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review" in on, "must re-run on pull_request_review (#65 stale-green)"
    types = on["pull_request_review"]["types"]
    assert "submitted" in types, "a new review is added via a 'submitted' review"
    # edited/dismissed change which threads exist or their state → re-evaluate.
    for t in ("edited", "dismissed"):
        assert t in types, f"pull_request_review must fire on {t}"


def test_reruns_when_a_review_comment_is_created():
    """A created review comment is a freshly-opened thread — the dangerous add-after-green case."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review_comment" in on, "must re-run on pull_request_review_comment (#65)"
    types = on["pull_request_review_comment"]["types"]
    for t in ("created", "edited", "deleted"):
        assert t in types, f"pull_request_review_comment must fire on {t}"


def test_workflow_dispatch_still_present_with_required_pr_number():
    """Manual re-check stays — and it must demand a pr_number (no PR in a manual payload)."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["pr_number"]["required"] is True


# ---------------------------------------------------------------------------
# Deliberately EXCLUDED events — each is a false-coverage no-op for this gate.
# Pinned so a future agent doesn't "close the gap" by adding a trigger that never
# updates the required status.
# ---------------------------------------------------------------------------

def test_does_not_trigger_on_issue_comment():
    """issue_comment runs on the DEFAULT-branch SHA, so its check-run never attaches to the PR
    head where branch protection looks — it would re-run but never update the required status.
    Listing it is false coverage; keep it out."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "issue_comment" not in on, (
        "issue_comment re-runs on the default-branch SHA and can't update the PR's required "
        "status — a false-coverage no-op; do not add it"
    )


def test_does_not_trigger_on_pull_request_review_thread():
    """pull_request_review_thread (resolved/unresolved) is a WEBHOOK event, NOT a supported
    GitHub Actions trigger — listing it would silently never fire."""
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(RT_WF.read_text()))
    assert "pull_request_review_thread" not in on, (
        "pull_request_review_thread is not a valid Actions trigger (webhook-only); it would "
        "never fire — do not add it"
    )


# ---------------------------------------------------------------------------
# Tamper-resistance: run the TRUSTED base copy of the gate script, never the PR's.
# ---------------------------------------------------------------------------

def test_checkout_pins_trusted_base_ref():
    """The review/comment triggers default to the PR MERGE ref (refs/pull/N/merge), which
    contains the PR's edits to review-threads.sh. The checkout must pin `ref` to the trusted
    PR BASE branch so the base copy of the script runs under every trigger."""
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    step = next(
        s for s in wf["jobs"]["review-threads"]["steps"] if str(s.get("uses", "")).startswith(
            "actions/checkout"
        )
    )
    assert step["with"]["ref"] == "${{ github.event.pull_request.base.ref }}", (
        "checkout must pin to the trusted PR base branch (base.ref), not the default merge ref"
    )


def test_checkout_never_uses_pr_head_ref():
    """Hard rule: NEVER checkout the PR head/merge under the privileged trigger — that runs
    PR-controlled code. Asserted against executable (non-comment) lines so the documented
    prohibition in the header doesn't trip the check."""
    code = _executable_lines(RT_WF.read_text())
    for forbidden in ("head.sha", "head.ref", "/merge", "pull_request.merge_commit_sha"):
        assert forbidden not in code, f"checkout must not reference {forbidden} (PR-controlled)"


def test_still_runs_the_vendored_gate_script():
    """The job runs the catalog script (the same one ci/ship reuses), not an inline reimpl."""
    code = _executable_lines(RT_WF.read_text())
    assert "bash ci/review-threads/review-threads.sh" in code


def test_token_stays_read_only():
    """No write scope is needed to COUNT threads; keep the privileged-trigger token minimal."""
    yaml = pytest.importorskip("yaml")
    perms = yaml.safe_load(RT_WF.read_text())["permissions"]
    assert perms.get("contents") == "read"
    assert perms.get("pull-requests") == "read"
    assert "write" not in set(perms.values()), "review-threads needs no write scope"


# ---------------------------------------------------------------------------
# PR-number resolution per event — every trigger that reaches the JOB must resolve a PR.
# ---------------------------------------------------------------------------

def test_pr_number_resolution_covers_every_trigger():
    """Trace the PR_NUMBER expression against each trigger's payload shape:

      • pull_request_target / pull_request_review / pull_request_review_comment
            -> all carry github.event.pull_request.number  (first operand)
      • workflow_dispatch
            -> no pull_request in the payload -> inputs.pr_number  (fallback operand)

    issue_comment (which would need github.event.issue.number) is intentionally NOT a trigger
    here (see test_does_not_trigger_on_issue_comment), so the expression needs no issue branch.
    """
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    step = next(
        s for s in wf["jobs"]["review-threads"]["steps"] if "review-threads.sh" in str(s.get("run", ""))
    )
    expr = step["env"]["PR_NUMBER"]
    assert "github.event.pull_request.number" in expr, "must read the PR number from the event"
    assert "inputs.pr_number" in expr, "manual runs must fall back to the pr_number input"

    # Every trigger that actually reaches the job is covered by one of those two operands.
    on = _on_block(wf)
    pr_object_events = {"pull_request_target", "pull_request_review", "pull_request_review_comment"}
    for ev in pr_object_events:
        assert ev in on, f"{ev} carries github.event.pull_request — must be a trigger to be covered"
    assert "workflow_dispatch" in on, "the inputs.pr_number fallback only applies on a manual run"


def test_gate_script_reads_pr_number_env():
    """The env-resolved PR number must actually feed the script (PR_NUMBER or $1)."""
    text = RT_SH.read_text()
    assert "PR_NUMBER" in text, "script must consume the PR_NUMBER env the workflow sets"


def test_readme_documents_the_excluded_events():
    """Doc/behaviour drift guard: the README must explain why issue_comment / review_thread
    are out, so the exclusion reads as deliberate, not forgotten."""
    text = RT_README.read_text()
    assert "issue_comment" in text
    assert "pull_request_review_thread" in text
    assert "#65" in text


# ---------------------------------------------------------------------------
# Run coalescing: the new triggers fan out (1 review + N comments per submit), so the
# workflow must dedupe per PR.
# ---------------------------------------------------------------------------

def test_concurrency_coalesces_runs_per_pr():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(RT_WF.read_text())
    conc = wf.get("concurrency")
    assert conc, "expected a top-level concurrency block to coalesce the fan-out runs"
    assert "github.event.pull_request.number" in conc["group"], "group must key per PR"
    assert conc["cancel-in-progress"] is True


# ---------------------------------------------------------------------------
# Behavioral: drive the SHIPPED review-threads.sh with a stubbed `gh` so the real control
# flow (PR-number requirement, page summing, fail-closed) and the real jq selection logic
# (isResolved / isOutdated / IGNORE_OUTDATED) are exercised — not just YAML structure.
# ---------------------------------------------------------------------------

def _stub_gh(bin_dir: Path, *, stdout: str = "0", exit_code: int = 0, jq_json: str | None = None) -> None:
    """Write a fake `gh` onto PATH.

    - Default mode: echo `stdout` (a per-page count, possibly multi-line) and exit `exit_code`
      — exercises the script's summing + fail-closed branch without touching the network.
    - jq mode (`jq_json` set): extract the script's OWN `--jq` filter from argv and run the
      canned `jq_json` through the real `jq`, so the shipped selection logic is what runs.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if jq_json is None:
        script = f"#!/usr/bin/env bash\nprintf '%s\\n' {_shq(stdout)}\nexit {exit_code}\n"
    else:
        # Pull the value after `--jq` out of the args, then feed canned JSON to real jq.
        script = (
            "#!/usr/bin/env bash\n"
            "filter=''\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--jq" ]; then filter="$2"; shift 2; continue; fi\n'
            "  shift\n"
            "done\n"
            f"printf '%s' {_shq(jq_json)} | jq \"$filter\"\n"
        )
    gh = bin_dir / "gh"
    gh.write_text(script)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in the generated bash stub."""
    return "'" + s.replace("'", "'\\''") + "'"


def _run_gate(bin_dir: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}" + env["PATH"]
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(RT_SH), *args], capture_output=True, text=True, env=env, timeout=30
    )


def _threads_json(*threads: dict) -> str:
    import json

    nodes = ",".join(
        json.dumps({"isResolved": t["resolved"], "isOutdated": t.get("outdated", False)})
        for t in threads
    )
    return (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{'
        '"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[' + nodes + "]}}}}}"
    )


def test_script_requires_a_pr_number(tmp_path: Path):
    """No PR number (arg or env) -> usage error exit 2, before any gh call."""
    res = _run_gate(tmp_path / "bin")
    assert res.returncode == 2, res.stderr
    assert "Usage" in res.stderr


def test_script_passes_with_zero_unresolved(tmp_path: Path):
    _stub_gh(tmp_path / "bin", stdout="0")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 0, res.stderr
    assert "PASS" in res.stdout


def test_script_fails_with_unresolved_threads(tmp_path: Path):
    _stub_gh(tmp_path / "bin", stdout="2")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "unresolved review thread" in res.stderr


def test_script_sums_unresolved_across_pages(tmp_path: Path):
    """--paginate yields one count per page; the script must sum them (2 + 1 = 3)."""
    _stub_gh(tmp_path / "bin", stdout="2\n1")
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "3 unresolved" in res.stderr


def test_script_fails_closed_on_gh_error(tmp_path: Path):
    """A gh/API error must FAIL (exit 1), never silently pass — an unresolved PR could merge."""
    _stub_gh(tmp_path / "bin", stdout="", exit_code=4)
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1
    assert "failing closed" in res.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_script_jq_counts_outdated_unresolved_by_default(tmp_path: Path):
    """The shipped default jq counts an unresolved thread even when GitHub marked it OUTDATED:
    1 resolved + 1 unresolved(outdated) + 1 unresolved -> 2 unresolved -> FAIL."""
    json_payload = _threads_json(
        {"resolved": True},
        {"resolved": False, "outdated": True},
        {"resolved": False, "outdated": False},
    )
    _stub_gh(tmp_path / "bin", jq_json=json_payload)
    res = _run_gate(tmp_path / "bin", "123")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "2 unresolved" in res.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_script_jq_ignore_outdated_skips_outdated(tmp_path: Path):
    """IGNORE_OUTDATED=1 drops the outdated-but-unresolved thread -> only 1 counts -> FAIL,
    and the same payload PASSES when the only unresolved thread is outdated."""
    payload = _threads_json(
        {"resolved": False, "outdated": True},
        {"resolved": False, "outdated": False},
    )
    _stub_gh(tmp_path / "bin", jq_json=payload)
    res = _run_gate(tmp_path / "bin", "123", env_extra={"IGNORE_OUTDATED": "1"})
    assert res.returncode == 1
    assert "1 unresolved" in res.stderr

    only_outdated = _threads_json({"resolved": False, "outdated": True})
    _stub_gh(tmp_path / "bin", jq_json=only_outdated)
    res2 = _run_gate(tmp_path / "bin", "123", env_extra={"IGNORE_OUTDATED": "1"})
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "PASS" in res2.stdout
