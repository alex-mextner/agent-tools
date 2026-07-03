"""Tests for the orchestrator-stays-thin agent-hook (pre-write + pre-bash).

Covers the doctrine's four cases for BOTH points: BLOCK (a repeat code write / impl bash by
the main thread), ALLOW (docs path / read-only one-liner / first-offense WARN), SUBAGENT-EXEMPT
(agent_id present), and the ESCAPE hatch (env+reason and inline sentinel; reasonless still
blocks). Hermetic: the warn/block tier marker dir is redirected into tmp_path via env, so the
two-call warn→block sequence is exercised without touching the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_orchestrator_stays_thin.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "orchestrator-stays-thin"
    / "orchestrator_stays_thin.py"
)
_spec = importlib.util.spec_from_file_location("orchestrator_stays_thin", _HOOK)
assert _spec and _spec.loader
ost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ost)


def _run(event, monkeypatch, marker_dir: Path, env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Redirect the tier marker dir into the test sandbox and re-read the module constant.
    monkeypatch.setenv("ORCH_THIN_MARKER_DIR", str(marker_dir))
    monkeypatch.setattr(ost, "MARKER_DIR", marker_dir)
    for k in ("ALLOW_ORCHESTRATOR_WORK", "ALLOW_ORCHESTRATOR_WORK_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = ost.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK (warn first, then block on repeat) ───────────────────────────────────────────

def test_block_code_write_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    # first offense → WARN (allow + message)
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    # repeat in the same cwd within TTL → BLOCK
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE
    assert _decision(out2) == "block"
    assert "delegate" in json.loads(out2)["message"].lower()


def test_block_impl_bash_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "sed -i 's/a/b/' f && npm run build && echo done"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── ALLOW (docs / read-only / non-offending) ───────────────────────────────────────────

def test_allow_docs_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/README.md"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_allow_docs_dir_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/docs/plan.json"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_allow_read_only_bash(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status && ls -la"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── SUBAGENT-EXEMPT ────────────────────────────────────────────────────────────────────

def test_subagent_exempt_code_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"agent_id": "sub-7", "file_path": "/repo/src/a.ts"}}
    # even on a repeat it must allow, because a subagent does the actual work
    _run(event, monkeypatch, tmp_path / "m")
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_empty_agent_id_does_not_exempt(tmp_path, monkeypatch):
    """An EMPTY/whitespace `args.agent_id` is NOT a subagent — it must NOT exempt the
    orchestrator. A blank signal can't relax the gate, so a repeat impl write still BLOCKs."""
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"agent_id": "   ", "file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_only_args_agent_id_exempts_not_top_level_or_tool_input(tmp_path, monkeypatch):
    """TRUST BOUNDARY regression guard (agent-tools#115). `_is_subagent` reads ONLY
    `args.agent_id` — the single surface lib/cc_hook_bridge sanitizes (T2 precedence: it drops
    any model/tool_input-supplied copy and NEVER writes a top-level `agent_id`). This gate uses
    agent_id to RELAX (exempt a subagent), so a forged agent_id sitting ANYWHERE ELSE must NOT
    exempt the orchestrator:
      - at the event TOP LEVEL (the old `or event.get('agent_id')` fallback — an unsanitized
        relax-surface, now dropped), and
      - nested under `args.tool_input` (a model-controllable surface).
    With no real `args.agent_id` the orchestrator gate applies and a repeat impl write BLOCKs.
    If a future edit widened the read to either surface, the orchestrator could self-exempt and
    this test would catch it."""
    event = {"point": "pre-write", "cwd": "/repo", "agent_id": "top-level-decoy",
             "args": {"file_path": "/repo/src/a.ts",
                      "tool_input": {"agent_id": "nested-decoy"}}}
    _run(event, monkeypatch, tmp_path / "m")  # warn (NOT exempted by the decoys)
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ESCAPE ─────────────────────────────────────────────────────────────────────────────

def test_escape_env_reason_allows(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"ALLOW_ORCHESTRATOR_WORK": "1", "ALLOW_ORCHESTRATOR_WORK_REASON": "trivial tweak"},
    )
    assert c == 0 and _decision(out) == "allow"


def test_escape_inline_sentinel_allows_bash(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "sed -i s/a/b/ f && echo x && echo y  # orchestrator-ok: one-off"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_reasonless_override_still_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m", {"ALLOW_ORCHESTRATOR_WORK": "1"})  # no reason
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B1: a chain that merely STARTS read-only is judged on its full content ───────────────

@pytest.mark.parametrize("command", [
    "git status && sed -i 's/a/b/' f.py",  # read-only prefix + in-place edit
    "ls; npm run build",                   # read-only prefix + build
])
def test_read_only_prefix_chain_blocks_on_repeat(command, tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_bare_read_only_still_allows(tmp_path, monkeypatch):
    """A single unchained read-only command keeps its carve-out (no warn, no block)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


# ── B7: a bare redirect is not implementation ────────────────────────────────────────────

def test_plain_redirect_is_not_implementation(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "python foo.py > out.log"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # still not implementation
    assert c2 == 0 and _decision(out2) == "allow"


# ── #80: a FULLY read-only pipe of ANY length is never blocked ───────────────────────────

@pytest.mark.parametrize("command", [
    "find . -name foo | grep bar | head",      # the live-session repro
    "tail -100 log | grep err | wc -l",        # 3-segment inspection
    "find . | grep x | head -5",               # 3 segments, trailing args
    "cat a.txt | grep -i warn | grep -v ok | head -20",  # 4 segments
])
def test_read_only_pipe_any_length_allows(command, tmp_path, monkeypatch):
    """A pipe where EVERY segment is read-only inspection must never warn or block, even
    with 3+ segments (it tripped `len(CHAIN.findall()) >= 2` before #80)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "find . | grep x | sed -i 's/a/b/' f.py",     # read-only segments + in-place edit
    "tail -50 log | grep err | npm install pkg",  # read-only segments + installer
    "cat a | grep b | tee out.txt",               # read-only segments + tee write
])
def test_read_only_pipe_with_one_impl_segment_blocks_on_repeat(command, tmp_path, monkeypatch):
    """One build/edit segment anywhere in an otherwise-read-only pipe still blocks (#80)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_read_only_pipe_with_heredoc_segment_blocks_on_repeat(tmp_path, monkeypatch):
    """A heredoc inside an otherwise-read-only pipe still blocks (#80)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > f\nbody\nEOF\ngrep x f | head"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_pipe_with_non_read_only_segment_unchanged(tmp_path, monkeypatch):
    """A pipe with a segment that is neither read-only nor build/edit keeps the old
    `>= 2 operators is implementation` behavior — the carve-out only covers ALL-read-only."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "find . | python score.py | head"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    "cat tee.log",                       # `tee` is the FILE being read, not a write
    "grep tee notes.txt",                # `tee` is the search NEEDLE
    "cat Cargo.toml",                    # build-manifest is an inspection target
    "head package.json",                 # ditto
    "grep dep Cargo.toml | head",        # read-only pipe, build-token as an argument
])
def test_read_only_with_build_token_argument_allows(command, tmp_path, monkeypatch):
    """A build/edit token appearing only as the ARGUMENT/needle of a read-only command keeps
    the carve-out (#80 review #1) — judgement is per-segment-HEAD, not a whole-string scan.
    `tee`/`sed -i` are unanchored in BUILD_EDIT, so a whole-string veto would mis-flag these."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


def test_single_read_only_via_new_path_still_allows(tmp_path, monkeypatch):
    """The old single-command carve-out is now a subset of _is_all_read_only — pin it (#80
    review #2): a bare `git status` still routes through the new path and is never blocked."""
    assert ost._is_all_read_only("git status") is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "git status && ls && cat x",          # all-read-only && chain (>= 2 operators)
    "ls; cat foo.txt; grep err foo.txt",  # all-read-only ; chain
])
def test_read_only_non_pipe_chain_allows(command, tmp_path, monkeypatch):
    """The carve-out covers ANY operator, not just `|` — a read-only `&&`/`;` chain of any
    length is inspection too (#80 review #3). `git` is narrowly scoped in READ_ONLY_BASH
    (status/log/diff/show/branch only), so `git add && git commit && git push` is NOT covered
    — see test_git_mutating_chain_still_blocks_on_repeat."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


def test_git_mutating_chain_still_blocks_on_repeat(tmp_path, monkeypatch):
    """`git` is narrowly scoped to read-only subcommands, so a mutating git chain is NOT
    waved through as all-read-only and still blocks on repeat (#80 review #1 — no security
    regression: add/commit/push do not match READ_ONLY_BASH)."""
    assert ost._is_all_read_only("git add -A && git commit -m x && git push") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "git add -A && git commit -m x && git push"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_build_edit_head_with_read_only_needle_blocks(tmp_path, monkeypatch):
    """ANCHOR INVARIANT (#80 review #1): the per-segment-HEAD contract holds only because
    READ_ONLY_BASH is head-anchored (`^\\s*`). A segment whose HEAD is a build/edit command
    (`sed -i ...`) but whose ARGUMENT contains a read-only word (`grep.py`) must NOT be waved
    through. If READ_ONLY_BASH were ever de-anchored, `.search()` would match the `grep` needle
    mid-segment and silently allow an in-place edit — this test fails loud on that regression."""
    cmd = "find . | sed -i 's/a/b/' grep.py"
    assert ost._is_all_read_only(cmd) is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": cmd}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── T3: heredoc-to-file is implementation ────────────────────────────────────────────────

def test_heredoc_to_file_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > f\nbody\nEOF"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── B3: a NotebookEdit carrying only notebook_path is judged ─────────────────────────────

def test_notebook_path_only_code_write_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"notebook_path": "/repo/src/explore.ipynb"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_notebook_path_under_docs_allows(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"notebook_path": "/repo/docs/notes.ipynb"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── B5: tiers are keyed by (cwd, point) — a pre-write warn does NOT prime a pre-bash block ─

def test_pre_write_warn_does_not_prime_pre_bash_block(tmp_path, monkeypatch):
    marker = tmp_path / "m"
    write_ev = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    bash_ev = {"point": "pre-bash", "cwd": "/repo",
               "args": {"command": "sed -i s/a/b/ f && echo x && echo y"}}
    out1, _e1, c1 = _run(write_ev, monkeypatch, marker)  # pre-write WARN
    assert c1 == 0 and _decision(out1) == "allow"
    # the FIRST pre-bash offense in the same cwd must still only WARN (independent tier)
    out2, _e2, c2 = _run(bash_ev, monkeypatch, marker)
    assert c2 == 0 and _decision(out2) == "allow"
    # the SECOND pre-bash offense now blocks (its own tier matured)
    out3, _e3, c3 = _run(bash_ev, monkeypatch, marker)
    assert c3 == ost.BLOCK_EXIT_CODE and _decision(out3) == "block"


# ── #5: BUILD_EDIT tool tokens must be anchored at a command head, not match as a needle ─

@pytest.mark.parametrize("command", [
    "cat notes.md | grep npm",        # npm is a grep needle, not the command
    "git log | rg yarn",              # yarn is an rg needle
    "find . -name cargo.toml | wc -l",  # cargo.toml is a find argument
])
def test_build_tool_as_pipe_needle_is_not_implementation(command, tmp_path, monkeypatch):
    """A build-tool NAME appearing as an argument/needle in an inspection pipe must NOT be
    classified as implementation — only a build tool at the COMMAND head counts (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    # never primes a block, because it is not an offending action at all
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", ["npm run build", "cargo build", "ls; npm run build"])
def test_real_build_at_head_blocks_on_repeat(command, tmp_path, monkeypatch):
    """A real build tool at the command head (or after a separator) is implementation (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_sed_in_place_anywhere_still_implementation(tmp_path, monkeypatch):
    """`sed -i` keeps its position-free signal: it is implementation even unchained (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "sed -i 's/a/b/' f.py"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── #159: `gh ship` is RELEASE, not implementation — the orchestrator's own carve-out ────

@pytest.mark.parametrize("command", [
    "gh ship 605",                                             # bare ship (pins the trivial case)
    "gh ship 605 2>&1 | tail -30 | grep -i merged",            # ship + read-only plumbing (2 ops)
    "gh ship 605 --skip-ci | tail -20; git log --oneline -3",  # ship + post-merge inspection
    "GH_PAGER=cat GH_TOKEN=x gh ship 605 | tail -5 | head -1",  # env-var prefixes on the ship head
    "cd /repo && gh ship 605 | tail -40",                      # `cd` companion segment
    "git status && gh ship 605 && git log --oneline -1",       # read-only companions via &&
    "gh ship 605 > ship.log 2>&1",                             # the sanctioned logging shape
    "gh ship 605 --repo alex-mextner/agent-tools",             # cross-repo ship: `--repo <owner/repo>`
    "gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok",  # + the no-screenshot flag
    "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3",  # all three
    "gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3",  # quoted `;` in a reason arg (F2/P2)
])
def test_gh_ship_release_chain_allows(command, tmp_path, monkeypatch):
    """A `gh ship` line (plus read-only/`cd` plumbing) is the sanctioned RELEASE action at
    ORCHESTRATOR altitude (#159): the auto-mode classifier denies subagents the gated merge,
    so the orchestrator must run it itself — it must never warn and never prime the block
    tier (the warn-then-block flapping on repeated ships was the gated-ship deadlock)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    assert "message" not in json.loads(out1)  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' f.py && gh ship 605",   # ship does not launder an in-place edit
    "npm run build && gh ship 605",          # ...nor a build
    "gh ship 605; tee out.txt; ls",          # ...nor a tee write
    "gh ship 605 $(sed -i 's/a/b/' f)",      # ...nor an edit hidden in a substitution
    "cd $(npm run build) && gh ship 605",    # ...nor a build inside the cd companion
    "gh ship 605 & sed -i 's/a/b/' f.py",    # ...nor behind a single `&` (CHAIN can't split it)
    "cd $(git push origin main) && gh ship 605 | tail -3",  # non-BUILD_EDIT mutation in $() (2 ops)
    "gh ship 605 & git push origin main; ls; cat x",  # bare `&` hides a push in the ship segment
    "cat <(git push origin main) && gh ship 605 | tail -3",  # process substitution smuggles a push
    "gh ship 605 | find . -delete | tail -3",           # read-only HEAD, mutating flag (#80 gap)
    "gh ship 605 | env git push origin main | tail -3",  # env wrapper launders a push
    "gh ship 605; git branch -D tmp; ls",                # git branch mutating form
    "gh ship 605 | find . -fprintf evil.sh 'x' | tail -3",  # find file-WRITE primary (review F1)
    "cd-clean && gh ship 605 | tail -3",                    # `cd-clean` is a different command (P1)
])
def test_gh_ship_does_not_launder_impl_segments(command, tmp_path, monkeypatch):
    """The carve-out is per-segment: a `gh ship` tacked onto an implementation chain must NOT
    exempt the rest of the line (#159) — only all-(ship|read-only|cd) lines are release.
    BUILD_EDIT and `$()`/backtick substitutions are vetoed on the WHOLE string (like heredoc),
    so a mutation smuggled INSIDE a ship/cd segment still blocks — pre-carve-out these fell to
    the whole-string BUILD_EDIT / >=2-operators checks, and the exemption must not regress
    that (review P1 + P2)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    "python x.py | grep 'gh ship' | python y.py",  # needle in a non-read-only pipe
    "gh shipwreck 1 | python x.py | head",         # word boundary: not the ship subcommand
])
def test_gh_ship_needle_or_prefix_word_is_not_exempt(command, tmp_path, monkeypatch):
    """`gh ship` counts only at a segment HEAD (argv), never as a substring in text (#159) —
    a grep needle or a `gh ship*`-prefixed other word must not self-exempt a chain."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_ship_bare_amp_zero_operator_line_keeps_inherited_allow(tmp_path, monkeypatch):
    """Document the hook's ACTUAL decision on `gh ship 605 & git push origin main`: allowed —
    but NOT via the release carve-out (the bare-`&` veto rejects it; pinned in the predicate
    test). CHAIN does not split a single `&`, so the line has ZERO chain operators and falls
    under the ordinary judgement, which never flagged 0-operator non-build lines (`python
    x.py & git push` behaves identically) — inherited pre-#159 behavior, not a regression.
    If CHAIN ever learns to split `&`, this documents the boundary to re-judge."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh ship 605 & git push origin main"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


def test_gh_ship_with_non_build_mutation_companion_blocks(tmp_path, monkeypatch):
    """A companion mutation that BUILD_EDIT does not know about (git push) still does not
    ride the carve-out: it is not a benign head, so a >=2-operator chain stays
    implementation-shaped and warn-then-blocks exactly as before (#159 review)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh ship 605 && git push origin main && git push --tags"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_ship_with_heredoc_still_blocks(tmp_path, monkeypatch):
    """A heredoc anywhere vetoes the release carve-out, exactly as it vetoes read-only (#159)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > notes\nbody\nEOF\ngh ship 605 | tail -5"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_sanctioned_release_predicate_is_head_anchored():
    """Pin the predicate surface (#159): exemption requires a real `gh ship` segment HEAD —
    an all-read-only pipe that merely MENTIONS `gh ship` is allowed via the read-only path,
    not via the release carve-out, and an `echo gh ship` head exempts nothing."""
    assert ost._is_sanctioned_release_chain("gh ship 605 | tail -3 | head -1") is True
    assert ost._is_sanctioned_release_chain("grep 'gh ship' log | head | wc -l") is False
    assert ost._is_sanctioned_release_chain("echo gh ship") is False
    assert ost._is_sanctioned_release_chain("gh ship 605 && git push") is False
    # READ_ONLY_BASH is `^`-anchored, so a read-only WORD mid-segment (`writer cat`) does not
    # qualify a companion — .search cannot match past the head (#159 review).
    assert ost._is_sanctioned_release_chain("gh ship 605 | writer cat | tool grep") is False
    # Command substitutions are vetoed wholesale — even a read-only one forfeits the
    # carve-out and falls back to the ordinary judgement (#159 review P2).
    assert ost._is_sanctioned_release_chain("cd $(git rev-parse --show-toplevel) && gh ship 605") is False
    # A bare background `&` is vetoed (CHAIN cannot split it), but redirect `&`s are not:
    # `2>&1` must stay a first-class ship shape (#159 review P3).
    assert ost._is_sanctioned_release_chain("gh ship 605 & git push origin main") is False
    assert ost._is_sanctioned_release_chain("gh ship 605 2>&1 | tail -3") is True
    # Process substitution is vetoed like `$()` — either direction (#159 review P4).
    assert ost._is_sanctioned_release_chain("gh ship 605 | cat <(git push origin main)") is False
    assert ost._is_sanctioned_release_chain("tee >(wc -l) | gh ship 605") is False
    # The #80 inherited gap (read-only head, mutating form) is not extended to ship lines —
    # but a genuinely read-only find companion still qualifies (#159 review P5).
    assert ost._is_sanctioned_release_chain("gh ship 605 | find . -delete | tail -3") is False
    assert ost._is_sanctioned_release_chain("gh ship 605 | find . -name x | head") is True
    # The head anchor ends at `gh ship`, so trailing FLAGS ride along untouched. The gate does NOT
    # validate `gh ship`'s own arguments — that is ship's job — it only recognises a release line
    # and gets out of the way, so whatever forms the orchestrator's `gh ship` accepts must pass
    # (task #23): `--repo <owner/repo>` (the `/` in the slug is not a chain operator) and
    # `--no-screenshot-ok`. (A given repo's ship.sh may itself reject a flag; that is ship's error
    # to raise downstream, not the gate's to pre-empt.)
    assert ost._is_sanctioned_release_chain("gh ship 605 --repo alex-mextner/agent-tools") is True
    assert ost._is_sanctioned_release_chain(
        "gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok") is True
    assert ost._is_sanctioned_release_chain(
        "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3") is True
    # A find file-WRITE primary is a mutation just like `-delete` — it must not ride a ship line
    # (review F1); and `cd-clean`/`cd/foo` are NOT the `cd` companion (argv-boundary anchor, P1).
    assert ost._is_sanctioned_release_chain("gh ship 605 | find . -fprintf evil.sh 'x' | tail -3") is False
    assert ost._is_sanctioned_release_chain("cd-clean && gh ship 605 | tail -3") is False
    # A quoted metachar in a ship reason must not forfeit the carve-out (quote-aware split + veto,
    # review F2/P2): a quoted `;` is not a segment split, a quoted `&` does not trip the bare-`&`
    # veto, and a quoted `$(`/build token does not trip the substitution/build vetoes.
    assert ost._is_sanctioned_release_chain("gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3") is True
    assert ost._is_sanctioned_release_chain('gh ship 605 --title "a & b" | tail -3') is True
    assert ost._is_sanctioned_release_chain("gh ship 605 --note 'ran $(build) earlier' | tail -3") is True


# ── coordinator: report (`tg`) + read-only verification are orchestrator altitude, not impl ──────

@pytest.mark.parametrize("command", [
    "tg 'msg'",                                           # plain report (the mandatory case)
    "tg --format html '<b>done</b>' | tail -3",           # report + read-only plumbing (a pipe)
    "tg --format html 'x' | tail -3 | grep merged",       # 2-operator report chain (used to block)
    "gh pr view 5 | jq .title | head -1",                 # PR verification piped through jq/head
    "tg done; gh pr view 5; gh run list",                 # report + verify, 3 segments
    "df -h | grep /dev | head",                           # read-only system verification
    "lsblk | grep sda | wc -l",                           # ...another
    "cd /repo && tg 'done' | tail",                       # `cd` companion on a report line
])
def test_report_or_verify_chain_allows(command, tmp_path, monkeypatch):
    """`tg` reporting and read-only PR/CI/system verification are the orchestrator's OWN altitude —
    a multi-step report/verify chain must never warn OR prime a block (coordinator directive)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    assert "message" not in json.loads(out1), command  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


@pytest.mark.parametrize("command", [
    "tg done && sed -i 's/a/b/' f.py",    # report does not launder an in-place edit
    "tg done; tee out.txt; ls",           # ...nor a tee write
    "tg done $(sed -i 's/a/b/' f)",       # ...nor an edit hidden in a substitution
    "tg done; git branch -D tmp; ls",     # ...nor a git branch mutation (>=2 operators)
    "gh pr view 5 && git push && git push --tags",  # ...nor a >=2-operator push chain
])
def test_report_or_verify_does_not_launder_impl(command, tmp_path, monkeypatch):
    """The report/verify carve-out is per-segment like the release one: a `tg`/gh-read head does not
    exempt a mutation elsewhere on the line (coordinator directive) — it warn-then-blocks."""
    assert ost._is_report_or_verify_chain(command) is False, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_report_verify_predicate_surface():
    """Pin the predicate: `tg`/gh-read heads qualify, curl/ssh do NOT (not reliably read-only)."""
    assert ost._is_report_or_verify_chain("tg 'x' | tail") is True
    assert ost._is_report_or_verify_chain("gh pr checks 5 | grep fail | head") is True
    assert ost._is_report_or_verify_chain("git log | grep x | head") is False  # no tg/gh-read head
    # curl can POST and ssh runs any remote command — neither is a sanctioned read-only verb, so a
    # chain fronted by them is NOT waved through (they keep the escape hatch).
    assert ost._is_report_or_verify_chain("curl -X POST http://h/api | tg done | tail") is False
    assert ost._is_report_or_verify_chain("ssh root@h 'df -h; lsblk' | tg done") is False
    # The carve-out itself never launders a bare-`&` background push (the veto rejects it); A's
    # pre-existing ordinary judgement decides the rest — the carve-out just does not exempt it.
    assert ost._is_report_or_verify_chain("tg done & git push origin main; ls") is False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
