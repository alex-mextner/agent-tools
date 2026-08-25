"""Tests for skills/universal/gh-graphql/ghgql — the read-only-by-default `gh api graphql` wrapper.

`ghgql` builds a `gh api graphql` argv and (by default) refuses a mutation before running it. These
tests exercise it with `--dry-run` (which prints the `gh` argv and runs nothing), so no `gh` binary
or network is needed. The read-only-refusal must stay consistent with the merge guard: a query that
only NAMES a merge token inside a string literal is a READ and must be allowed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_GHGQL = Path(__file__).resolve().parents[1] / "skills" / "universal" / "gh-graphql" / "ghgql"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_GHGQL), *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_ghgql_is_executable():
    assert _GHGQL.is_file()
    assert _GHGQL.stat().st_mode & 0o111, "ghgql must be executable so a symlinked skill can run it"


def test_dry_run_inline_query_builds_graphql_argv():
    r = _run(["--dry-run", "query { viewer { login } }"])
    assert r.returncode == 0, r.stderr
    assert "api graphql" in r.stdout
    assert "query=query" in r.stdout.replace("\\", "")


def test_dry_run_forwards_variables_and_paginate():
    r = _run(["--dry-run", "--paginate", "-F", "owner=cli", "query($owner:String!){ x }"])
    assert r.returncode == 0, r.stderr
    assert "--paginate" in r.stdout
    assert "owner=cli" in r.stdout


def test_dry_run_file_backed_query_is_read_and_inlined(tmp_path):
    """ghgql reads a `-q @file` query itself (#303) so its own mutation-refusal can inspect the
    actual text, and so what it execs is the inlined query, not an unread `@file` marker."""
    qfile = tmp_path / "query.graphql"
    qfile.write_text("query { viewer { login } }")
    r = _run(["--dry-run", "-q", f"@{qfile}"])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.replace("\\", "")
    assert "query=query { viewer { login } }" in argv
    assert "@" not in argv.split("query=", 1)[1]


def test_file_backed_mutation_is_refused_by_default(tmp_path):
    qfile = tmp_path / "mutation.graphql"
    qfile.write_text('mutation { mergePullRequest(input:{pullRequestId:"x"}){ id } }')
    r = _run(["-q", f"@{qfile}"])
    assert r.returncode == 2
    assert "mutation" in r.stderr.lower()


def test_file_backed_mutation_allowed_with_allow_mutation(tmp_path):
    qfile = tmp_path / "mutation.graphql"
    qfile.write_text("mutation { addComment(input:{}){ id } }")
    r = _run(["--dry-run", "--allow-mutation", "-q", f"@{qfile}"])
    assert r.returncode == 0, r.stderr
    assert "addComment" in r.stdout


def test_missing_query_file_errors():
    r = _run(["-q", "@/no/such/file.graphql"])
    assert r.returncode == 2
    assert "cannot read query file" in r.stderr.lower()


def test_dry_run_stdin_query_is_read_and_inlined():
    r = _run(["--dry-run", "-"], stdin="query { viewer { login } }")
    assert r.returncode == 0, r.stderr
    argv = r.stdout.replace("\\", "")
    assert "query=query { viewer { login } }" in argv
    assert "@" not in argv.split("query=", 1)[1]


def test_stdin_mutation_is_refused_by_default():
    r = _run(["-"], stdin='mutation { mergePullRequest(input:{pullRequestId:"x"}){ id } }')
    assert r.returncode == 2
    assert "mutation" in r.stderr.lower()


def test_stdin_mutation_allowed_with_allow_mutation():
    r = _run(["--dry-run", "--allow-mutation", "-"], stdin="mutation { addComment(input:{}){ id } }")
    assert r.returncode == 0, r.stderr
    assert "addComment" in r.stdout


def test_bare_mutation_is_refused_by_default():
    r = _run(["mutation { mergePullRequest(input:{pullRequestId:\"x\"}){ id } }"])
    assert r.returncode == 2
    assert "mutation" in r.stderr.lower()


@pytest.mark.parametrize("token", ["enablePullRequestAutoMerge", "enqueuePullRequest", "mergeBranch"])
def test_all_landing_mutations_refused_by_default(token):
    r = _run([f"mutation {{ {token}(input:{{}}){{ id }} }}"])
    assert r.returncode == 2


def test_allow_mutation_opt_in_lets_a_write_through():
    r = _run(["--dry-run", "--allow-mutation", "mutation { addComment(input:{}){ id } }"])
    assert r.returncode == 0, r.stderr
    assert "api graphql" in r.stdout


def test_readonly_search_naming_merge_token_is_allowed():
    """The exact read SKILL.md advertises as allowed: a merge token only inside a string literal is
    data, so ghgql must NOT refuse it (consistent with the merge guard's de-stringing)."""
    r = _run(["--dry-run", 'query { search(query:"mergePullRequest" type:ISSUE){ nodes{ __typename } } }'])
    assert r.returncode == 0, r.stderr
    assert "api graphql" in r.stdout


def test_readonly_block_string_naming_merge_token_is_allowed():
    """A merge token inside a GraphQL block string in a read-only query is data — allowed."""
    r = _run(["--dry-run", 'query { """doc: enablePullRequestAutoMerge""" viewer { login } }'])
    assert r.returncode == 0, r.stderr
    assert "api graphql" in r.stdout


def test_no_query_errors():
    r = _run(["--dry-run"])
    assert r.returncode == 2
    assert "no query" in r.stderr.lower()


def test_readonly_comment_naming_merge_token_is_over_refused_not_bypassed():
    """The wrapper's own standalone guard is a COARSE, string-only approximation (it does not
    strip `#` comments at all — see the comment above the `sed` pipeline in `ghgql`): a read-only
    query whose `#` comment merely NAMES a merge token is (over-)refused, not allowed. This is a
    deliberate, documented trade-off (over-refuse is the SAFE direction; the Python hook is the
    authoritative gate and correctly allows this exact case via its comment-aware stripper — see
    `test_graphql_readonly_comment_naming_merge_token_is_allowed` in test_block_raw_pr_merge.py).
    Locks that the wrapper never silently starts ALLOWING a comment-hidden case instead."""
    query = "query {\n  # remember to check mergePullRequest availability\n  viewer { login }\n}"
    r = _run(["--dry-run", query])
    assert r.returncode == 2
    assert "mutation" in r.stderr.lower()


def test_native_f_query_field_is_treated_as_the_query():
    """`ghgql -f query='…'` (the native gh spelling) is the query, not a forwarded variable — it must
    build a single query field and not die with 'no query given' or duplicate the field."""
    r = _run(["--dry-run", "-f", "query=query { viewer { login } }"])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.replace("\\", "")
    assert argv.count("query=") == 1


def test_unknown_flag_is_forwarded_verbatim():
    r = _run(["--dry-run", "--slurp", "query { viewer { login } }"])
    assert r.returncode == 0, r.stderr
    assert "--slurp" in r.stdout
