"""Tests for the orchestrator-stays-thin agent-hook (pre-write + pre-bash).

Covers the doctrine's four cases for BOTH points: BLOCK (a repeat code write / impl bash by
the main thread), ALLOW (docs path / read-only one-liner / first-offense WARN), SUBAGENT-EXEMPT
(agent_id present), and the deny-by-default Telegram hatch escalation (the old
ALLOW_ORCHESTRATOR_WORK env + `# orchestrator-ok:` sentinel are DEAD; on a would-be BLOCK,
RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN with a written justification asks tg-ctl and allows
only on exit 0, a bare `1` denies). Hermetic: the warn/block tier marker dir is redirected into
tmp_path via env, so the two-call warn→block sequence is exercised without touching the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_orchestrator_stays_thin.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
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
    for k in ("ALLOW_ORCHESTRATOR_WORK", "ALLOW_ORCHESTRATOR_WORK_REASON", "RIG_ORCHESTRATOR_ONLY",
              "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN"):
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


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline) ──────────────────

def test_old_env_escape_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """ALLOW_ORCHESTRATOR_WORK=1 + _REASON as a real env pair must NO LONGER allow a repeat
    offense — the self-service bypass was removed (replaced by the Telegram hatch)."""
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"ALLOW_ORCHESTRATOR_WORK": "1", "ALLOW_ORCHESTRATOR_WORK_REASON": "trivial tweak"},
    )
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(tmp_path, monkeypatch):
    """A `# orchestrator-ok: …` sentinel on a repeat impl bash must still BLOCK — the inline
    sentinel is gone."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "sed -i s/a/b/ f && echo x && echo y  # orchestrator-ok: one-off"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── Telegram hatch escalation (deny-by-default; only a would-be BLOCK consults it) ─────────

def _fake_tg_ctl(path: Path, body: str):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _repeat_event() -> dict:
    return {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}


def test_hatch_unset_blocks_and_names_env_var(tmp_path, monkeypatch):
    event = _repeat_event()
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")  # repeat → block
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    event = _repeat_event()
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m",
                      {"RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN": "1"})  # repeat → deny
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nprintf approved\nexit 0\n")
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    event = _repeat_event()
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN": "One-char fix in a generated file."},
    )
    assert c == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    event = _repeat_event()
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN": "One-char fix in a generated file."},
    )
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


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


# ── sanctioned orchestration (tg / review / git worktree list) is NEVER blocked, even chained ─────

@pytest.mark.parametrize("command", [
    "tg 'shipped'",                                # report (the mandatory case)
    "tg 'a' && tg 'b'",                            # report chain
    "git worktree list",                           # worktree inspection
    "review diff",                                  # multi-model review CLI
    "review diff && tg done",                       # review + report chain
    "git worktree list | grep wt | head",           # worktree inspection piped into read-only filter
    "dev start web",                                # configured dev/e2e lifecycle
    "dev list",                                     # inspect configured/running dev targets
    "dev status smoke",                             # e2e/dev progress/status
    "dev logs smoke --tail 50",                     # configured logs, not raw docker logs
    "dev e2e run smoke",                            # first-class e2e run
    "dev e2e status smoke",                         # first-class e2e status
    "dev e2e logs smoke",                           # first-class e2e logs
    "dev has-script --repo-only test",               # read-only script existence probe
    "dev run test",                                  # project-scoped rig.yaml scripts
    "dev run --repo-only test",                      # repo-owned hook/ship test runner
    "dev stop --port 5173",                          # project-scoped dev process control
    "dev stop --pgid 5001",                          # validated dev/e2e process group stop
    "dev env --add-project ../api",                  # session-scoped multi-project setup
])
def test_orchestration_chain_never_blocks(command, tmp_path, monkeypatch):
    """`tg` / `review` / `git worktree list` / known `dev` commands are orchestration, never
    implementation — a chain of only these (plus read-only tails) must not warn OR block. `gh` is
    deliberately EXCLUDED now (tg#7103): see test_gh_is_now_delegated_warn_then_blocks."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


# ── tg#7103: ALMOST ALL `gh` is DELEGATED — CI/PR verification warn-then-blocks ────────────────
# `gh ship <PR#>` is the ONE exception, restored (agent-tools#159, tg#9977) — see
# test_gh_ship_carve_out_allows / test_gh_delegation_judgement_matrix /
# test_orchestrator_carve_out_acceptance_matrix below.

@pytest.mark.parametrize("command", [
    "gh ship",                                     # ship with NO PR number — still delegated
    "gh ship abc",                                 # ship with a non-numeric argument
    "gh ship --repo o/r 638",                      # a flag BEFORE the PR number — not the narrow shape
    "gh pr merge 638",                             # the OTHER merge path — never carved out
    "gh pr view 638",                              # PR verification (bare)
    "gh pr checks 638",                            # CI verification (bare)
    "gh pr list && gh pr view 5",                  # PR inspection chain
    "gh pr checks 5 | grep fail",                  # gh read piped into a read-only filter
    "gh run list",                                 # CI status (bare)
    "gh run list && gh run view 9 && tg done",     # 3-segment gh chain
    "gh api repos/o/r/pulls | jq '.[].number'",    # gh api GET piped into jq
    "gh api repos/o/r/issues -X POST -f title=x",  # gh api mutation
    "gh api graphql -f query='mutation{x}'",       # graphql mutation
    "gh api repos/o/r/issues --method GET -f state=open",  # gh api GET with fields (still delegated)
])
def test_gh_is_now_delegated_warn_then_blocks(command, tmp_path, monkeypatch):
    """ALMOST ALL `gh` — PR/CI verification, api (GET or mutation), `gh pr merge`, and any `gh ship`
    shape that is NOT a bare `<PR#>` — is implementation the orchestrator must delegate to a
    subagent (Alex tg#7103). It is NOT in ORCH_ALLOW and IS a gh impl-signal, so a single unchained
    gh command warn-then-blocks exactly like `git commit`. (The one exception, `gh ship <PR#>`, is
    covered separately below — agent-tools#159.)"""
    assert ost._seg_is_impl_signal(command.split("&&")[0].split("|")[0].strip()) is True, command
    assert ost.ORCH_ALLOW.search(command) is None, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_gh_is_not_inline_allowed():
    """`gh pr`/`gh run`/`gh api` (every gh subcommand except the narrow `gh ship <PR#>` shape) are
    NOT inline-allowed orchestration — they are implementation."""
    assert ost._is_all_inline_allowed("gh pr checks 5 && tg 'done'") is False
    assert ost._is_implementation_bash("gh pr checks 5") is True
    assert ost._is_implementation_bash("gh ship abc") is True    # non-numeric — still delegated
    # `gh ship <PR#>` IS now inline-allowed (agent-tools#159) — see test_gh_ship_carve_out_allows
    assert ost._is_all_inline_allowed("gh ship 5 && tg 'done'") is True
    assert ost._is_implementation_bash("gh ship 5") is False
    # `tg`/`review` still ARE inline-allowed orchestration
    assert ost._is_all_inline_allowed("tg 'done' && review diff") is True


def test_gh_subagent_is_exempt(tmp_path, monkeypatch):
    """A dispatched subagent (`agent_id` present) still runs `gh ship`/`gh pr` freely — the gate
    governs the orchestrator only, and the subagent is the one meant to ship/verify (tg#7103)."""
    for command in ("gh ship 638", "gh pr checks 638"):
        event = {"point": "pre-bash", "cwd": "/repo",
                 "args": {"agent_id": "sub-1", "command": command}}
        _run(event, monkeypatch, tmp_path / "m")  # even on a repeat it must allow
        out, _e, c = _run(event, monkeypatch, tmp_path / "m")
        assert c == 0 and _decision(out) == "allow", command


def test_path_qualified_gh_is_delegated():
    """A path-qualified gh head (`/usr/bin/gh pr checks`) is normalized by basename and delegated
    too — the SAME normalization git gets (`/usr/bin/git commit`), closing the asymmetry a bare
    `^gh\\b` regex left open (Opus review). `gh` as a needle stays exempt; `gh-foo` is a different
    command. A path-qualified `gh ship <PR#>` is normalized the SAME way and gets the carve-out
    (agent-tools#159) — `_is_gh_ship_command` uses the identical basename normalization."""
    assert ost._is_gh_command("/usr/bin/gh ship 605") is True
    assert ost._is_gh_command("/opt/homebrew/bin/gh pr checks 5") is True
    assert ost._is_implementation_bash("/usr/bin/gh pr checks 5 | tail -3") is True
    assert ost._is_implementation_bash("/usr/bin/gh ship 605 | tail -3") is False
    assert ost._is_gh_ship_command("/usr/bin/gh ship 605") is True
    # a genuine needle / different command must NOT be swept in
    assert ost._is_gh_command("cat gh.md") is False
    assert ost._is_gh_command("grep 'gh ship' log") is False
    assert ost._is_gh_command("gh-foo bar") is False  # `gh-foo` is not `gh` (basename-exact)


def test_gh_unbalanced_quotes_are_conservative():
    """An UNBALANCED-quote gh segment shlex cannot parse must still register as gh (block), the
    conservative direction the old `gh api` path took — not silently pass (Opus review)."""
    assert ost._is_gh_command("gh ship 605 'oops") is True          # head regex fallback
    assert ost._is_gh_command("/usr/bin/gh pr view 5 'oops") is True  # ...path-qualified too
    assert ost._is_implementation_bash("gh ship 605 'oops") is True
    # a NON-gh unbalanced segment must NOT be mis-flagged as gh by the fallback
    assert ost._is_gh_command("echo 'oops") is False


def test_gh_env_prefix_is_delegated_after_strip():
    """`_is_gh_command` handles env-prefixes ITSELF in both branches (shlex + fallback), so it is
    correct even if a caller forgets to `_strip_wrappers` — and a timeout-WRAPPED head still
    delegates end-to-end via the caller's strip. `_is_gh_ship_command` skips the SAME env-prefixes
    (agent-tools#159), so an env-prefixed `gh ship <PR#>` gets the carve-out too."""
    assert ost._is_gh_command("GH_PAGER=cat gh pr checks 605") is True        # no pre-strip needed
    assert ost._is_gh_command("GH_PAGER=cat GH_TOKEN=x gh pr checks 605") is True  # multiple prefixes
    assert ost._is_gh_command(ost._strip_wrappers("GH_PAGER=cat gh pr checks 605")) is True
    assert ost._is_gh_command(ost._strip_wrappers("timeout 60 gh pr checks 5")) is True
    assert ost._is_gh_command("FOO=bar echo x") is False   # env-prefix on a non-gh head
    assert ost._is_implementation_bash("GH_PAGER=cat GH_TOKEN=x gh pr checks 605") is True
    # ...but an env-prefixed `gh ship <PR#>` gets the carve-out (agent-tools#159)
    assert ost._is_gh_ship_command("GH_PAGER=cat GH_TOKEN=x gh ship 605") is True
    assert ost._is_implementation_bash("GH_PAGER=cat GH_TOKEN=x gh ship 605") is False
    # env-prefix AND an unbalanced quote together: _strip_wrappers bails on the bad quote, so the
    # env-prefix survives to the fallback regex, which skips leading VAR=val before the gh head.
    # An unbalanced-quote ship segment is UNPARSEABLE by `_is_gh_ship_command` too — it falls back
    # to the general (still-delegated) gh deny, exactly like `_is_gh_command`'s own fallback.
    assert ost._is_implementation_bash("GH_PAGER=cat gh ship 605 'oops") is True
    assert ost._is_gh_command("GH_PAGER=cat GH_TOKEN=x gh ship 605 'oops") is True
    assert ost._is_gh_ship_command("GH_PAGER=cat GH_TOKEN=x gh ship 605 'oops") is False
    # ...and a non-gh env-prefixed unbalanced segment is still NOT mis-flagged as gh
    assert ost._is_gh_command("FOO=bar echo 'oops") is False


def test_dev_head_does_not_launder_impl_tail(tmp_path, monkeypatch):
    """`dev` is allowed only as its own sanctioned segment; a real build chained after it
    is still implementation-shaped and warn-then-blocks."""
    assert ost._is_all_inline_allowed("dev run test && npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev run test && npm run build"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_dev_run_only_known_safe_scripts_are_orchestration(tmp_path, monkeypatch):
    """`dev run test` is sanctioned, but `dev run <anything>` must not become a blanket
    implementation bypass for configured scripts with side effects."""
    assert ost._is_all_inline_allowed("dev run test") is True
    assert ost._is_all_inline_allowed("dev run build") is False
    assert ost._is_implementation_bash("dev run build") is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "dev run build"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_dev_e2e_is_allowed_inside_command_substitution():
    assert ost._dev_segment_is_unknown("dev e2e run smoke") is False
    assert ost._is_implementation_bash('tg "$(dev e2e status smoke)"') is False


def test_dev_unknown_subcommand_is_not_allowlisted(tmp_path, monkeypatch):
    """A blanket `dev` head would launder arbitrary argv; only known orchestration
    subcommands get the carve-out."""
    assert ost._is_all_inline_allowed("dev npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev npm run build && cat out"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", ["dev help", "dev e2e help"])
def test_dev_help_name_is_not_a_fake_subcommand(command):
    assert ost._dev_segment_is_allowed(command) is False
    assert ost._dev_segment_is_unknown(command) is True


def test_dev_e2e_unknown_nested_subcommand_is_not_allowlisted(tmp_path, monkeypatch):
    """`dev e2e` is first-class, but it cannot launder arbitrary nested argv."""
    assert ost._is_all_inline_allowed("dev e2e npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev e2e npm run build && cat out"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_head_with_impl_tail_still_blocks_on_repeat(tmp_path, monkeypatch):
    """A chain mixing gh with real work is judged on its full content — `gh pr view && git commit`
    blocks on repeat (both segments are now implementation)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh pr view 5 && git commit -m x"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_read_only_head_with_gh_tail_now_blocks(tmp_path, monkeypatch):
    """DIRECTION-REVERSAL guard: a read-only head with `gh` as the ONLY impl in the tail
    (`git status && gh pr view 5`) used to be ALLOWED (gh-read was orchestration) and must now
    warn-then-block — the whole point of tg#7103. Pins that the reversal holds regardless of
    segment order."""
    assert ost._is_implementation_bash("git status && gh pr view 5") is True
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "git status && gh pr view 5"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_prewrite_opt_out_follows_target_repo(tmp_path, monkeypatch):
    """The orchestrator opt-out for a pre-write follows the TARGET file's repo, not cwd (codex
    round 4). cwd is an opted-OUT repo, but the write TARGETS a strict (default-on) repo → still
    gated (blocks on repeat)."""
    strict = tmp_path / "strict"
    (strict / "src").mkdir(parents=True)
    (strict / "rig.yaml").write_text("agent_hooks:\n  all: true\n")  # no opt-out → default ON
    optout = tmp_path / "optout"
    optout.mkdir()
    (optout / "rig.yaml").write_text("agent_hooks:\n  orchestrator_only: false\n")
    event = {"point": "pre-write", "cwd": str(optout),
             "args": {"file_path": str(strict / "src" / "a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")  # warn (gated because TARGET repo is strict)
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_gh_pr_checkout_not_whitelisted(tmp_path, monkeypatch):
    """`gh pr checkout` mutates the local worktree/branch — it is NOT in the read-only/orchestration
    allow-list, so a chain built around it is judged on its full content (codex P2)."""
    assert ost._is_all_inline_allowed("gh pr checkout 5 && gh pr view 6 && tg x") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh pr checkout 5 && gh pr view 6 && tg x"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_needle_in_read_only_pipe_allows(tmp_path, monkeypatch):
    """ANCHOR INVARIANT: `gh`/`tg` as an ARGUMENT of a read-only command is not orchestration —
    `git log | rg gh` stays allowed because the segment HEADS are read-only, not because of `gh`."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git log | rg gh"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── agent-tools#159: the allow-list must NOT launder a mutation past its head-anchor ─────────
# The head-anchored allow-list (`gh ship`/`gh pr`/`tg`/`review`/read-only) once waved through a
# mutation smuggled where the head cannot see it: a command/process substitution, a bare `&`
# background, a `git branch <arg>`, or a find delete/exec/file-write primary. Each of these was a
# REGRESSION vs the pre-existing hook (which blocked them all). Pin them shut.

@pytest.mark.parametrize("command", [
    "gh ship 605 $(sed -i 's/a/b/' f)",              # edit hidden in a command substitution
    "gh ship 605 & sed -i 's/a/b/' f.py",            # edit behind a bare `&` (not a chain split)
    "gh ship 605 & git push origin main; ls; cat x",  # push behind a bare `&`
    "cat <(git push origin main) && gh ship 605 | tail -3",  # push in a process substitution
    "gh ship 605; git branch -D tmp; ls",            # `git branch` mutating form
    "gh ship 605; git -C /repo branch -D tmp; ls",   # ...incl. the `git -C <dir> branch` form
    "gh ship 605 | find . -delete | tail -3",        # find delete primary
    "gh ship 605 | find . -fprintf evil.sh 'x' | tail",  # find file-WRITE primary
    "tg 'done' && npm run build",                    # a build does not ride a `tg` prefix
])
def test_allow_list_does_not_launder_smuggled_mutation(command, tmp_path, monkeypatch):
    """A benign orchestration head must not exempt a mutation the head-anchor cannot see — each of
    these warn-then-blocks, exactly as the pre-#159 hook did (regression guard)."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    "cd /repo && tg 'done' | tail -40",              # `cd` companion (was wrongly blocked)
    "cd /repo && review diff | tail -3",             # cd + review companion
    "tg 'saw a & b later' | tail",                   # a quoted `&` must not trip the bare-`&` veto
    "tg 'fix; reship' | tail",                       # a quoted `;` must not split the segment
    "git branch",                                    # a bare `git branch` LIST stays read-only
])
def test_allow_list_covers_cd_and_quoted_reason(command, tmp_path, monkeypatch):
    """The `cd` companion and quote-aware handling must keep a legit orchestration line allowed —
    it must never warn or prime a block. (`gh` lines no longer ride this — they are delegated;
    the coverage now uses `tg`/`review`, which stay sanctioned.)"""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow", command


def test_seg_and_inline_allowed_predicate_surface():
    """Pin the predicate surface for the #159 hardening at the unit level."""
    # cd is an allowed companion; `cd-clean`/`cd/foo` (argv boundary) are NOT.
    assert ost._seg_is_allowed("cd /repo") is True
    assert ost._seg_is_allowed("cd-clean") is False
    assert ost._seg_is_allowed("cd/foo") is False
    # find mutating primaries and `git branch <arg>` forfeit the allow-list; bare reads keep it.
    assert ost._seg_is_allowed("find . -delete") is False
    assert ost._seg_is_allowed("find . -fprintf out.sh x") is False
    assert ost._seg_is_allowed("git branch -D tmp") is False
    assert ost._seg_is_allowed("git -C /repo branch -D tmp") is False
    assert ost._seg_is_allowed("git branch") is True
    # `gh` is delegated (tg#7103): any OTHER gh line is implementation, chained or not.
    assert ost._is_implementation_bash("gh pr checks 605 --title 'a & b' | tail") is True
    assert ost._is_implementation_bash("gh pr checks 605 & git push") is True
    # ...but the narrow `gh ship <PR#>` shape is the one sanctioned exception (agent-tools#159):
    # a quoted `&`/`;` in a trailing arg does not change the verdict — it is not a real chain
    # split (quote-aware), and the ship carve-out does not care what follows the PR number.
    assert ost._seg_is_allowed("gh ship 605") is True
    assert ost._seg_is_impl_signal("gh ship 605") is False
    assert ost._is_implementation_bash("gh ship 605 --title 'a & b' | tail") is False
    assert ost._is_implementation_bash("gh ship 605 --note 'a; b' | tail") is False
    # a REAL mutation elsewhere on the line still blocks — the ship exemption does not launder it
    assert ost._is_implementation_bash("gh ship 605 $(sed -i x)") is True
    assert ost._is_implementation_bash("gh ship 605 & git push") is True
    # A smuggled mutation behind a still-sanctioned `tg` head is caught by the substitution-inner
    # scan / `&` split, NOT by a blanket veto in _is_all_inline_allowed — which honestly reports
    # head-allowance, so a benign read-only substitution pipe still passes it (the #80 fix).
    assert ost._is_implementation_bash("tg done $(sed -i x)") is True
    assert ost._is_implementation_bash("tg done & git push") is True
    assert ost._is_implementation_bash("tg 'a & b' | tail") is False
    assert ost._is_all_inline_allowed("cat $(find . -name x) | grep k | head") is True


def test_read_only_pipe_with_benign_substitution_not_blocked():
    """#80 invariant: a read-only pipe of ANY length is never blocked, even carrying a benign
    substitution — the substitution must not fall it into the >=3 fallback (Opus review)."""
    for cmd in ["cat $(find . -name conf.yaml) | grep -i key | head",
                "git log $(git merge-base a b) | grep foo | head"]:
        assert ost._is_implementation_bash(cmd) is False, cmd


def test_orchestrator_only_env_falsy_values_disable(monkeypatch):
    """RIG_ORCHESTRATOR_ONLY accepts the same falsy set as rig.yaml (0/false/no/off) — an env-only
    `!= "0"` check surprised users who set `=false` expecting it to exempt the repo (Opus review)."""
    for val in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("RIG_ORCHESTRATOR_ONLY", val)
        assert ost._orchestrator_only_enabled("/repo") is False, val
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("RIG_ORCHESTRATOR_ONLY", val)
        assert ost._orchestrator_only_enabled("/repo") is True, val


# ── codex review: the veto must MAKE it offending, not just drop the fast path ───────────────

@pytest.mark.parametrize("command", [
    "tg done & git commit -m x",          # bare-`&` background smuggles a commit (own segment now)
    "gh ship 605 & git push origin main",  # ...a push
    "gh ship 605 $(gh api repos/o/r/x -X POST)",  # a gh api MUTATION inside a substitution head
    "gtimeout 60 git commit -m x",        # gtimeout wrapper stripped like timeout (macOS coreutils)
    "gtimeout 60 pytest tests/",          # ...on a test run
    "gtimeout -k 5 60 git commit -m x",   # gtimeout with an option + duration positional
])
def test_smuggled_mutation_is_now_offending(command, tmp_path, monkeypatch):
    """A bare-`&`/substitution/gtimeout form that previously only lost the fast path but stayed
    non-offending now warn-then-blocks — the veto MAKES it implementation (codex review)."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_all_gh_run_is_delegated():
    """ALL `gh run` (list/view/watch) is delegated now (tg#7103) — none is in ORCH_ALLOW, and each
    is a gh impl-signal. A gtimeout-wrapped `gh run` is delegated too (the wrapper is stripped,
    exposing the `gh` head); a gtimeout-wrapped `gh ship <PR#>` gets the SAME carve-out a bare one
    would, since wrapper-stripping exposes the identical `gh ship <PR#>` shape (agent-tools#159)."""
    for cmd in ("gh run list", "gh run view 9", "gh run watch 123"):
        assert ost.ORCH_ALLOW.search(cmd) is None, cmd
        assert ost._is_implementation_bash(cmd) is True, cmd
    assert ost._is_implementation_bash("gtimeout 60 gh run list | tail") is True
    assert ost._is_implementation_bash("gtimeout 60 gh ship 605 | tail") is False
    # a genuinely read-only substitution must still NOT over-block.
    assert ost._is_implementation_bash("cat $(ls -t | head -1)") is False


@pytest.mark.parametrize("command", [
    "df -h | grep /dev | head",           # read-only system-info verification pipe
    "lsblk | grep sda | wc -l",           # ...another
    "cat status.json | jq .title | head", # read-only file verification through jq/head
    "free -m | tail -1",                  # memory info
])
def test_read_only_system_verification_pipes_allow(command, tmp_path, monkeypatch):
    """Read-only system-info + filter verification tools (df/lsblk/free/…, jq/…) added to
    READ_ONLY_BASH so a multi-step verify pipe is not blocked by the >=3-segment rule (coordinator;
    SYNC with fix/159). (A `gh pr view` verify pipe is NOT here anymore — gh is delegated, tg#7103.)"""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow", command


# ── tg#5743: commits / pushes / test runs by the orchestrator ARE implementation ────────────

@pytest.mark.parametrize("command", [
    "git commit -m x",       # commit inline (was allowed before — 0 chains, not a build)
    "git push",              # push inline
    "pytest tests/",         # test run
    "go test ./...",         # go test run
    "python -m pytest tests/",  # wrapper form (codex P2c)
    "tox",                   # tox test run
    "env git commit -m x",   # env-prefix bypass (codex P1)
    "CI=1 pytest tests/",    # leading VAR= assignment bypass (codex P1)
    "GIT_AUTHOR_NAME=x git commit -m x",  # leading VAR= assignment bypass (codex P1)
    "env pytest tests/",     # env wrapper on a test run (codex P1)
    'FOO="bar baz" git commit -m x',  # QUOTED env value with a space (codex)
    "BAR='a b' pytest tests/",        # single-quoted env value with a space (codex)
    "uv run --with pytest pytest tests/",       # uv-wrapped test run (codex)
    "uv run --with pytest python -m pytest",    # uv-wrapped python -m pytest (codex)
    "uv run tox",                                # uv-wrapped tox
    "uv run --with=pytest python -m pytest",
    "uv run --python-preference system pytest tests/",
    "uv run --future-value-flag value pytest tests/",
    "uv run --future-value-flag value --with pytest python -m pytest",
    "uv run -p3.11 pytest tests/",
    "uv run -vp 3.11 python -m pytest",
    "env -u FOO git commit -m x",   # env option WITH operand (codex round 4)
    "env -C /tmp pytest tests/",    # env --chdir operand
    "timeout 60 pytest tests/",     # the MANDATED timeout wrapper (codex round 6)
    "timeout -k 5 60 git commit -m x",  # timeout with --kill-after option + duration
    "/usr/bin/env git commit -m x",     # absolute-path env (basename-matched)
    "time git push",                # time wrapper
    "nice -n 10 git commit -m x",   # nice with -n operand
    "uv run --env-file .env pytest tests/",  # uv --env-file operand skipped
    "env -i git commit -m x",       # env -i = ignore-env, NO operand (codex round 7)
    "env -i pytest tests/",         # env -i must not swallow pytest
    "python3 -m pytest tests/",     # python3 spelling — the repo's own form (codex round 8)
    "/usr/bin/python3 -m pytest",   # path-qualified python
    "python3 -m unittest discover",
    "git -C /repo commit -m x",     # git global option before subcommand (codex round 8)
    "git -c user.name=x commit -m x",
    "git --git-dir=.git --work-tree=. commit -m x",
    "/usr/bin/git commit -m x",     # path-qualified git
    "git -C /repo push",
])
def test_commit_push_test_blocks_on_repeat(command, tmp_path, monkeypatch):
    """Commits, pushes and test runs are a subagent's job — they warn-then-block for the
    orchestrator (Alex tg#5743), including behind a leading `env`/`VAR=val` wrapper (codex P1).
    Each was NOT caught before (a bare `git commit` had 0 chain operators and matched no build
    token; `env git commit` presented `env` as a read-only head)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    "uv run rig status",              # read-only tool, not a test
    "uv run rg pytest docs",         # SEARCHING for "pytest" — not running it (codex round 5)
    "uv run --frozen rig status",
    "uv run --with rg rg pytest docs",
])
def test_uv_run_readonly_tool_not_overblocked(command, tmp_path, monkeypatch):
    """The uv-test detector is shlex-based: only the COMMAND uv runs counts, not an argument.
    `uv run rig status` / `uv run rg pytest docs` must NOT be swept in as test runs (codex)."""
    assert ost._is_implementation_bash(command) is False, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "gh api repos/o/r/pulls | jq '.[] | select(.state==\"open\")'",  # quoted `|` inside jq
    "git log --oneline | rg 'feat|fix' | head",                       # quoted alternation
])
def test_quoted_pipe_inside_arg_not_split(command, tmp_path, monkeypatch):
    """A `|` inside a QUOTED argument (a jq program, an rg alternation) is not a chain operator —
    quote-aware splitting keeps such a read chain allowed instead of flapping (codex round 5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow", command


def test_gh_api_all_shapes_are_delegated():
    """Under tg#7103 the gh-api GET-vs-mutation distinction is gone — EVERY `gh api` shape (GET,
    POST, graphql, any method spelling) is delegated. `_gh_api_is_mutation` was removed with the
    carve-out; `gh api` is now caught by the general `_is_gh_command` impl-signal."""
    for cmd in (
        "gh api repos/o/r/issues --method=GET -f state=open",
        "gh api repos/o/r/issues -X GET -q '.[]'",
        "gh api repos/o/r --method GET --method POST",
        "gh api graphql -f query='mutation{x}'",
    ):
        assert ost._seg_is_impl_signal(cmd) is True, cmd
        assert ost._is_implementation_bash(cmd) is True, cmd
    assert not hasattr(ost, "_gh_api_is_mutation")  # helper removed with the carve-out


@pytest.mark.parametrize("command", [
    "timeout 60 git status",        # the mandated timeout wrapper on a READ-ONLY command
    "timeout 60 tg 'done' && timeout 60 review diff",  # timeout on orchestration, chained
    "/usr/bin/env git status",      # absolute-path env on a read-only command
    "git -C /r status && git -C /r log && git -C /r diff",  # read-only git with -C, 3-chain (round 8)
    "/usr/bin/git log --oneline",   # path-qualified read-only git
])
def test_wrappers_do_not_break_read_or_orchestration(command, tmp_path, monkeypatch):
    """Stripping wrappers must not turn a read/orchestration command into an offense — `timeout N
    git status` and `timeout N tg … && …` stay allowed, never warn/block (codex round 6). (`gh`
    behind a wrapper is now delegated — see test_all_gh_run_is_delegated.)"""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow", command


# ── tg#5743: per-repo opt-out (default ON, no regression) ───────────────────────────────────

def test_opt_out_via_env_allows_code_write(tmp_path, monkeypatch):
    """RIG_ORCHESTRATOR_ONLY=0 exempts a repo entirely — even a repeat code write allows."""
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m", {"RIG_ORCHESTRATOR_ONLY": "0"})  # would-be warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m", {"RIG_ORCHESTRATOR_ONLY": "0"})
    assert c == 0 and _decision(out) == "allow"


def test_opt_out_via_rigyaml_allows_code_write(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("agent_hooks:\n  orchestrator_only: false\n")
    event = {"point": "pre-write", "cwd": str(repo), "args": {"file_path": str(repo / "src/a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_blank_yaml_value_keeps_the_default(tmp_path, monkeypatch):
    """A blank value (`orchestrator_only:`) must return the DEFAULT, not silently disable a
    default-ON gate (codex P2). Default-off worktree_only stays off for a blank too."""
    assert ost._agent_hooks_bool(
        "agent_hooks:\n  orchestrator_only:\n", "orchestrator_only", default=True) is True
    assert ost._agent_hooks_bool(
        "agent_hooks:\n  worktree_only:\n", "worktree_only", default=False) is False


def test_default_on_still_blocks_when_no_rigyaml(tmp_path, monkeypatch):
    """No env, no rig.yaml → gate stays ON (opt-OUT default) — no regression vs prior always-on."""
    event = {"point": "pre-write", "cwd": str(tmp_path / "nowhere"),
             "args": {"file_path": str(tmp_path / "nowhere/src/a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── agent-tools#159 (restored, Alex tg#9977): `gh ship <PR#>` — narrow orchestrator carve-out ──

@pytest.mark.parametrize("command", [
    "gh ship 605",                                             # bare ship — the sanctioned shape
    "gh ship 605 2>&1 | tail -30 | grep -i merged",            # ship + read-only plumbing (2 ops)
    "gh ship 605 --skip-ci | tail -20; git log --oneline -3",  # ship + post-merge inspection
    "GH_PAGER=cat GH_TOKEN=x gh ship 605 | tail -5 | head -1",  # env-var prefixes on the ship head
    "cd /repo && gh ship 605 | tail -40",                      # `cd` companion segment
    "git status && gh ship 605 && git log --oneline -1",       # read-only companions via &&
    "gh ship 605 > ship.log 2>&1",                             # the logging shape
    "gh ship 605 --repo alex-mextner/agent-tools",             # cross-repo ship: `--repo <owner/repo>`
    "gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok",  # + the no-screenshot flag
    "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3",  # all three
    "gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3",  # quoted `;` in a reason arg
    "/usr/bin/gh ship 605",                                    # path-qualified head
    "gh ship 638 && tg 'shipped'",                             # ship + report (Fable review)
    'gh ship "605"',                                           # quoted-but-numeric PR (Fable review)
])
def test_gh_ship_carve_out_allows(command, tmp_path, monkeypatch):
    """A `gh ship <PR#>` line (any plumbing) is sanctioned for the orchestrator to run inline again
    (agent-tools#159, restored; Alex tg#9977) — matching AC1 of the original #159 issue: it exits 0
    (allow) on BOTH the first AND a repeat call, i.e. it never even primes the warn-block tier,
    exactly the same treatment `tg`/`review` already get. Read-only/`cd` companions ride along
    because they are independently allowed, not because ship "rescues" them."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    assert "message" not in json.loads(out1), command  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


def test_orchestrator_carve_out_acceptance_matrix(tmp_path, monkeypatch):
    """End-to-end acceptance for the restored carve-out (agent-tools#159, Alex tg#9977): the
    orchestrator running `gh ship <PR#>` is allowed (never even warns); the orchestrator running
    `gh pr merge`, raw `gh`, another code Edit/Write, or other implementation-shaped Bash still
    warn-then-blocks; a dispatched subagent (`agent_id` present) is unaffected either way and runs
    everything freely, on the first call AND a repeat. Each scenario gets its OWN marker dir — the
    warn/block tier is keyed by (cwd, point) only, so sharing one marker across scenarios would let
    an earlier offense's BLOCK bleed into a later, unrelated scenario's "first offense" check."""
    # orchestrator: gh ship <PR#> — allowed, never warns, never primes a block
    ship_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh ship 605"}}
    out1, _e1, c1 = _run(ship_event, monkeypatch, tmp_path / "ship")
    assert c1 == 0 and _decision(out1) == "allow" and "message" not in json.loads(out1)
    out2, _e2, c2 = _run(ship_event, monkeypatch, tmp_path / "ship")
    assert c2 == 0 and _decision(out2) == "allow"

    # orchestrator: gh pr merge — still warn-then-blocks (the OTHER merge path, never carved out)
    merge_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh pr merge 605"}}
    out1, _e1, c1 = _run(merge_event, monkeypatch, tmp_path / "merge")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(merge_event, monkeypatch, tmp_path / "merge")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"

    # orchestrator: raw gh (any other subcommand) — still warn-then-blocks
    raw_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh pr view 605"}}
    out1, _e1, c1 = _run(raw_event, monkeypatch, tmp_path / "raw")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(raw_event, monkeypatch, tmp_path / "raw")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"

    # orchestrator: a code Edit/Write — still warn-then-blocks (pre-write point, unaffected by gh)
    write_event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    out1, _e1, c1 = _run(write_event, monkeypatch, tmp_path / "write")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(write_event, monkeypatch, tmp_path / "write")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"

    # orchestrator: other implementation-shaped Bash (a commit) — still warn-then-blocks
    commit_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git commit -m x"}}
    out1, _e1, c1 = _run(commit_event, monkeypatch, tmp_path / "commit")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(commit_event, monkeypatch, tmp_path / "commit")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"

    # subagent: unaffected either way — gh ship AND gh pr merge both run freely, even on a repeat
    for i, cmd in enumerate(("gh ship 605", "gh pr merge 605")):
        sub_event = {"point": "pre-bash", "cwd": "/repo", "args": {"agent_id": "sub-1", "command": cmd}}
        marker = tmp_path / f"sub-{i}"
        _run(sub_event, monkeypatch, marker)
        out, _e, c = _run(sub_event, monkeypatch, marker)
        assert c == 0 and _decision(out) == "allow", cmd


def test_gh_ship_carve_out_does_not_vet_skip_ci(tmp_path, monkeypatch):
    """`gh ship 605 --skip-ci` matches the narrow ship shape and IS allowed by THIS gate, never
    warns — this gate recognizes the ship shape only, it does not vet ship's own flags. The
    `--skip-ci` bypass itself is a SEPARATE, independently-gated control: `ci/ship/ship.sh` refuses
    it deny-by-default and requires its own live-Telegram approval via
    `RIG_HATCH_REQUEST_SHIP_SKIP_CI` — this gate is not where that gets enforced (Opus review,
    pinning the intentional behavior explicitly rather than leaving it implicit)."""
    assert ost._is_gh_ship_command("gh ship 605 --skip-ci") is True
    assert ost._is_implementation_bash("gh ship 605 --skip-ci") is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh ship 605 --skip-ci"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow" and "message" not in json.loads(out1)
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


def test_skip_ci_hatch_still_exists_in_ship_sh():
    """Cross-check pin (Fable review): the previous test's safety argument for waving `--skip-ci`
    through THIS gate rests entirely on `ci/ship/ship.sh` owning its OWN deny-by-default hatch for
    it. Nothing in `orchestrator_stays_thin.py` couples to that fact — if `ship.sh`'s hatch were
    ever renamed, weakened, or removed, this hook would silently become an ungated inline path to
    a CI-skipping merge and every test in this file would keep passing. Pin the hatch's presence
    here so a change to `ship.sh` that breaks the assumption fails a test, not a security review.

    Pins the actual bash CONSTRUCT, not just prose (Fable review round 2: a plain substring/phrase
    grep can pass after the real gate is deleted, as long as a stray comment survives, and can fail
    on a harmless comment reword while the gate stays intact — the wrong signal in both directions).
    Round 3 tightened it further: a naive `body.index("fi")` after the unset-check false-matched
    the "fi" *inside the word* "justification" in an intervening `echo` line, proving the exact
    failure mode the round-2 version was trying to avoid — this version extracts the FUNCTION BODY
    (bounded by its own opening/closing braces) and locates the unset-check's `then`-block via a
    standalone `fi` keyword (its own whitespace-only line, not a bare substring), then
    requires a bare `exit 1` line strictly BEFORE that `fi` — an actual refusal in that specific
    branch, not `exit 1` appearing anywhere later in the function. Also pins that `SKIP_CI=1` (the
    `--skip-ci` flag's own state) actually calls the gate before any merge — so the coupling is to
    the REAL call graph, not just to two constructs existing independently and unwired."""
    ship_sh = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"
    text = ship_sh.read_text(encoding="utf-8")

    fn_match = re.search(r"^_skip_ci_hatch_gate\(\) \{\n(.*?)^\}", text, re.M | re.S)
    assert fn_match, "_skip_ci_hatch_gate() function not found in ship.sh"
    body = fn_match.group(1)

    assert '${RIG_HATCH_REQUEST_SHIP_SKIP_CI+x}' in body  # the actual "is unset" bash idiom
    unset_check = body.index('RIG_HATCH_REQUEST_SHIP_SKIP_CI+x')
    fi_match = re.search(r"^\s*fi\s*$", body[unset_check:], re.M)
    assert fi_match, "no standalone `fi` closing the unset-check if-block"
    then_block = body[unset_check:unset_check + fi_match.start()]
    assert re.search(r"^\s*exit 1\s*$", then_block, re.M), (
        "unset-check then-block does not end in a bare `exit 1` refusal"
    )

    # The call-graph pin (Fable review round 3: the earlier version only checked that
    # `_skip_ci_hatch_gate` appears somewhere AFTER the `if` line — it would still pass if the
    # call sat in the `else` branch, after the whole `if`, or was never called at all). Extract the
    # `if [ "$SKIP_CI" = "1" ]; then … fi` block by its own bounding `fi` and require the call
    # INSIDE it, before the actual merge (`run gh pr merge`) it's meant to gate.
    if_match = re.search(
        r'^if \[ "\$SKIP_CI" = "1" \]; then\n(.*?)\nfi$', text, re.M | re.S
    )
    assert if_match, "SKIP_CI=1 if-block not found in the expected shape"
    skip_ci_block = if_match.group(1)
    assert "_skip_ci_hatch_gate" in skip_ci_block, (
        "the skip-ci gate is not called inside the SKIP_CI=1 branch"
    )
    gate_call_idx = skip_ci_block.index("_skip_ci_hatch_gate")
    merge_idx = skip_ci_block.index("run gh pr merge")
    assert gate_call_idx < merge_idx, "the gate is not called BEFORE the admin merge"


def test_ship_carve_out_not_blocked_by_a_primed_tier(tmp_path, monkeypatch):
    """A `gh ship <PR#>` is evaluated as ALLOWED before the warn/block tier is ever consulted
    (`main()` only calls `_is_repeat` when `offending` is True) — so even after the SAME (cwd,
    point) marker has already been primed to BLOCK by an unrelated offense (`gh pr merge`), a
    subsequent `gh ship <PR#>` in that same cwd still allows cleanly (Fable review: pins that the
    carve-out is not just "unprimeable in isolation" but robust against an already-primed tier)."""
    marker = tmp_path / "m"
    merge_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh pr merge 605"}}
    _run(merge_event, monkeypatch, marker)  # first offense: WARN
    out, _e, c = _run(merge_event, monkeypatch, marker)  # repeat: BLOCK, tier now primed
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"

    ship_event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh ship 605"}}
    out, _e, c = _run(ship_event, monkeypatch, marker)  # SAME marker/cwd, already primed to BLOCK
    assert c == 0 and _decision(out) == "allow"
    assert "message" not in json.loads(out)


def test_gtimeout_wrapped_ship_allowed_end_to_end(tmp_path, monkeypatch):
    """A `gtimeout`-wrapped `gh ship <PR#>` gets the SAME carve-out a bare one would — the wrapper
    is stripped, exposing the identical `gh ship <PR#>` shape (agent-tools#159) — proven end-to-end
    via `_run` (not just the `_is_implementation_bash` unit level), with a 3-segment chain so
    `_seg_is_allowed` (the fast `_is_all_inline_allowed` path), not only the impl-signal fallback,
    must see the wrapper-stripped segment for the whole chain to pass (Fable review)."""
    command = "gtimeout 60 gh ship 605 2>&1 | tail -30 | grep -i merged"
    assert ost._is_all_inline_allowed(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow" and "message" not in json.loads(out1)
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
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
    "gh ship 605 || git push",                              # `||` is a chain split too (Opus review)
    "gh ship 605 --note `git push origin main`",            # backtick substitution (Opus review)
    "gh ship 605 > >(git push origin main)",                # process substitution (Opus review)
    "gh ship 605 --note <(git push origin main)",           # input-side process subst (Fable review)
    "gh ship 605 | gh pr merge 606",                        # a sibling gh MUTATION (Opus review)
    "gh ship 605; gh api repos/o/r/issues -X POST -f title=x",  # ...another sibling gh mutation
    "gh ship 605 |& git push origin main",                  # `|&` still splits into 2 (Opus review)
    "gh ship 605\ngit push origin main",                    # a NEWLINE chain-splits too (Fable review)
    "gh ship 605; ( git push )",                            # a `(...)` subshell group (Opus review)
    "gh ship 605 && ( git push )",                          # ...via `&&`
    "gh ship 605 | ( git push )",                           # ...via `|`
])
def test_gh_ship_does_not_launder_impl_segments(command, tmp_path, monkeypatch):
    """The allowance is per-segment: a `gh ship` tacked onto an implementation chain must NOT
    exempt the rest of the line (#159) — only all-(ship|read-only|cd) lines pass. A mutation
    smuggled where no head can see it (inside `$()`/`<()`/backticks, behind a bare `&`, a
    mutating find/git-branch form) is caught by the substitution-inner scan, the `&` split and
    the companion guards — the exemption must not regress what was blocked pre-carve-out."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_incidental_mutation_shaped_text_in_a_ship_segment_matches_tg_precedent():
    """A single segment whose TEXT happens to also match a mutation-shaped regex — `tee` is a bare
    word in `gh ship 605 tee`, tripping `BUILD_EDIT`'s unanchored `sed -i`/`tee` alternative — is
    still ALLOWED end-to-end, because `_seg_is_allowed`'s contract is "sanctioned HEAD/SHAPE", not
    "no mutation-shaped substring anywhere in the text". `gh ship <PR#>` intentionally matches the
    SAME pattern the pre-existing `tg`/`review` heads already have (see the parallel `tg` case
    below) — `tee` here is inert trailing text, never an executed command. The REAL protection
    against a genuine mutation is unconditional and elsewhere: a real chain operator makes it a
    SEPARATE segment (covered by `test_gh_ship_does_not_launder_impl_segments`'s `; tee out.txt;`
    case, which DOES block), and a LIVE substitution is caught by `_has_mutating_substitution`
    regardless of what this predicate says about the outer segment (Opus/Fable review — an earlier
    draft added a `not BUILD_EDIT.search(...)` guard to ONLY the ship branch, which made ship
    stricter than its own `tg` precedent for the identical false-positive class and broke the
    single-quoted-substitution test; reverted in favor of pinning the actual, consistent design)."""
    ship_segment = "gh ship 605 tee"
    assert ost._is_gh_ship_command(ship_segment) is True
    assert ost.BUILD_EDIT.search(ship_segment) is not None  # the argument text LOOKS like a mutation
    assert ost._seg_is_allowed(ship_segment) is True         # sanctioned shape wins over the text
    assert ost._is_implementation_bash(ship_segment) is False

    # the parallel, pre-existing `tg` case (unaffected by this carve-out, same underlying pattern)
    tg_segment = "tg 'saw $(git push) in logs'"
    assert ost.BUILD_EDIT.search(tg_segment) is not None
    assert ost._seg_is_allowed(tg_segment) is True
    assert ost._is_implementation_bash(tg_segment) is False


def test_gh_ship_variable_expansion_does_not_match(tmp_path, monkeypatch):
    """`gh ship $PR` / `gh ship "$PR_NUMBER"` do NOT match the carve-out (Fable review): the hook
    inspects the LITERAL, pre-expansion command text — no shell ever runs before this hook sees the
    string, so a genuinely-numeric PR passed through a shell variable is indistinguishable here from
    any other non-numeric token and still delegates. This is exactly the shape a future "helpful"
    relaxation might target, and it is a prominent, user-facing behavioral promise in the README
    ("pass the literal digits, not a variable") — pin it so a future change can't silently flip it."""
    assert ost._is_gh_ship_command("gh ship $PR") is False
    assert ost._is_gh_ship_command('gh ship "$PR_NUMBER"') is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "gh ship $PR"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs — delegated, not carved out
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_ship_trailing_bare_tokens_are_unrestricted():
    """A SECOND bare token after the PR number (`gh ship 605 606`) still matches the carve-out —
    only `argv[0]` (post-`gh`) is checked against `ship` and `argv[1]` against the digit pattern;
    everything after is genuinely unrestricted, including another bare number, not just flags
    (Fable review: the docs say "trailing flags/redirects unrestricted", which understates this).
    Safe by defense-in-depth even so: `ci/ship/ship.sh`'s OWN arg parser treats the PR number as
    "the lone bare arg" and refuses a SECOND one outright ("Multiple PR numbers … — pass one.",
    ci/ship/ship.sh — grepped below), so this hook being permissive here does not sanction an
    actual inline batch-merge; ship.sh's own parser is the second, independent gate."""
    assert ost._is_gh_ship_command("gh ship 605 606") is True
    assert ost._is_gh_ship_command("gh ship 605 606 607") is True
    assert ost._is_implementation_bash("gh ship 605 606") is False
    ship_sh_text = (
        Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"
    ).read_text(encoding="utf-8")
    # Pin the CONSTRUCT, not just the prose (same standard as the skip-ci cross-check above,
    # Fable review round 3): the "Multiple PR numbers" message must sit on a line that itself
    # `exit 1`s — an actual refusal, not merely a string that could survive after the refusal
    # logic was removed.
    error_line = next(
        (line for line in ship_sh_text.splitlines() if "Multiple PR numbers" in line), None
    )
    assert error_line is not None, "'Multiple PR numbers' refusal message not found in ship.sh"
    assert "exit 1" in error_line, "the 'Multiple PR numbers' line does not itself exit 1"


def test_gh_ship_find_mutation_text_falls_through_to_allowed():
    """A ship segment whose trailing TEXT contains a `find` mutating-primary substring
    (`--note 'find -delete'`) forfeits `_seg_is_allowed`'s FIND_MUTATION veto — but this is NOT
    itself an impl signal (`_seg_is_impl_signal` only checks BUILD_EDIT/uv/git-python/dev/gh, never
    FIND_MUTATION), so with no chain operators the line falls through every check to ALLOWED
    (Fable review: pins the intended, PRE-EXISTING outcome of this gray zone — not new to the ship
    carve-out, the identical pattern already exists for `tg`/`review`, see the parallel case below;
    the text is inert, never executed, and the REAL find-delete protection is chain-length/
    substitution scanning for an ACTUAL separate `find` command, covered by
    `test_gh_ship_does_not_launder_impl_segments`'s `| find . -delete |` case)."""
    ship_segment = "gh ship 605 --note 'find -delete'"
    assert ost.FIND_MUTATION.search(ship_segment) is not None
    assert ost._seg_is_allowed(ship_segment) is False       # the fast-path veto DOES forfeit it
    assert ost._is_implementation_bash(ship_segment) is False  # ...but the line is still allowed

    tg_segment = "tg 'find -delete'"                        # the same pre-existing pattern for tg
    assert ost.FIND_MUTATION.search(tg_segment) is not None
    assert ost._seg_is_allowed(tg_segment) is False
    assert ost._is_implementation_bash(tg_segment) is False


def test_gh_ship_find_mutation_text_flips_to_blocked_once_chained(tmp_path, monkeypatch):
    """The SAME `find`-mutating-shaped TEXT that falls through to allowed UNCHAINED (the test
    above) flips to warn-then-block once ANY read-only companion joins it — `_seg_is_allowed`'s
    FIND_MUTATION veto costs the fast path regardless of chain length, but the chain-length
    fallback (`>= 3` segments) only fires once there ARE 3+ segments (Fable review: the README
    prominently advertises specific "never warns, never blocks" ship+plumbing pipe shapes, so this
    flip — adding an inert read-only `| tail -3 | head -1` to an otherwise-fine ship line changes
    the verdict — is worth pinning explicitly, not left as an undocumented surprise). Pre-existing
    `tg`-precedent behavior, not introduced by the carve-out; the ship carve-out just makes it more
    user-visible given the README's own plumbing examples."""
    command = "gh ship 605 --note 'find -delete' | tail -3 | head -1"
    assert ost._is_implementation_bash(command) is True
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


# NOTE (rebase over #162): the old #162 test "bare-& zero-operator line keeps inherited allow"
# (`gh ship 605 & git push origin main` allowed) is deliberately DROPPED here: this branch's
# `_split_chain` splits a bare control `&`, so the smuggled `git push` is judged on its own
# segment and the line warn-then-blocks — pinned by test_smuggled_mutation_is_now_offending.
# Stricter than #162's inherited behavior, in the safe direction.


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


def test_gh_delegation_judgement_matrix():
    """`gh pr merge` and every OTHER gh subcommand stay DELEGATED (tg#7103); the narrow
    `gh ship <PR#>` shape is the one restored exception (agent-tools#159, tg#9977). Asserted on
    `_is_implementation_bash` (True = blocked/delegated, False = allowed)."""
    impl = ost._is_implementation_bash
    # `gh pr merge` — the OTHER merge path — is never carved out, regardless of plumbing/quoting.
    assert impl("gh pr merge 605 | tail -3 | head -1") is True
    assert impl("gh pr merge 605 --admin") is True
    assert impl("cd /repo && gh pr merge 605 | tail -3") is True
    # every gh ship <PR#> shape is now ALLOWED (False), including the plumbing forms below.
    assert impl("gh ship 605 | tail -3 | head -1") is False
    assert impl("gh ship 605 2>&1 | tail -3") is False
    assert impl("gh ship 605 --repo alex-mextner/agent-tools") is False
    assert impl("gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok") is False
    assert impl(
        "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3") is False
    assert impl("cd $(git rev-parse --show-toplevel) && gh ship 605") is False
    # quoted-metachar reasons do not change the verdict — no real chain split, ship still matches.
    assert impl("gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3") is False
    assert impl('gh ship 605 --title "a & b" | tail -3') is False
    # a SINGLE-quoted substitution in a trailing arg is literal text (never executes) — allowed,
    # same live/literal distinction `tg` already gets (test_double_quoted_substitution_is_live_and_judged).
    assert impl("gh ship 605 --note 'ran $(build) earlier' | tail -3") is False
    assert impl("gh ship 605 --note '$(git push origin main)' | tail -3") is False
    # ...but a DOUBLE-quoted substitution DOES execute — a real mutation inside it still blocks.
    assert impl('gh ship 605 --note "$(git push origin main)" | tail -3') is True
    # `gh ship` counts only at a segment HEAD (argv) — a needle in a read-only pipe is NOT a gh cmd
    assert impl("grep 'gh ship' log | head | wc -l") is False
    # a REAL companion mutation on the same line still blocks — the ship exemption is per-segment,
    # not per-line, and does not launder the rest of the chain.
    assert impl("gh ship 605 && git push") is True
    assert impl("gh ship 605 & git push origin main") is True
    assert impl("tee >(wc -l) | gh ship 605") is True  # tee is BUILD_EDIT
    # a non-numeric or missing PR-number argument falls through to the general (still-delegated) gh
    # deny — the exemption is on the exact `gh ship <PR#>` shape, not the word "ship".
    assert impl("gh ship") is True
    assert impl("gh ship abc") is True
    assert impl("gh ship --help") is True
    # ...and the same guards protect the STILL-sanctioned `tg`/`review`/read-only allow-list:
    assert impl("tg done | writer cat | tool grep") is True   # non-allowed heads, >=3 segments
    assert impl("tg done & git push origin main") is True     # bare `&` split exposes the push
    assert impl("tg done | cat <(git push origin main)") is True  # process-subst inner judged
    assert impl("cd $(git rev-parse --show-toplevel) && tg done") is False  # benign read-only subst
    assert impl("tg 'saw a & b' | tail") is False              # quoted metachar keeps the pass


# ── coordinator: report (`tg`) + read-only verification are orchestrator altitude, not impl ──────

@pytest.mark.parametrize("command", [
    "tg 'msg'",                                           # plain report (the mandatory case)
    "tg --format html '<b>done</b>' | tail -3",           # report + read-only plumbing (a pipe)
    "tg --format html 'x' | tail -3 | grep merged",       # 2-operator report chain (used to block)
    "cat status.json | jq .title | head -1",              # read-only file verification through jq
    "tg done; git status; git log --oneline -3",          # report + read-only verify, 3 segments
    "df -h | grep /dev | head",                           # read-only system verification
    "lsblk | grep sda | wc -l",                           # ...another
    "cd /repo && tg 'done' | tail",                       # `cd` companion on a report line
])
def test_report_or_verify_chain_allows(command, tmp_path, monkeypatch):
    """`tg` reporting and read-only system verification are the orchestrator's OWN altitude — a
    multi-step report/verify chain must never warn OR prime a block. (gh-based verification like
    `gh pr view` is NOT here anymore — gh is delegated to a subagent, tg#7103.)"""
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
    """The report/verify allowance is per-segment like the release one: a `tg`/gh-read head does
    not exempt a mutation elsewhere on the line (coordinator directive) — it warn-then-blocks."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_report_verify_allowlist_surface():
    """Pin the doctrine under tg#7103: `tg`/`review`/`git worktree list` are the sanctioned
    orchestration heads (ORCH_ALLOW). `gh` is NO LONGER one — it is delegated. `curl`/`ssh` were
    never sanctioned (curl can POST, ssh runs any remote command)."""
    assert ost.ORCH_ALLOW.search("tg 'x'") is not None
    assert ost.ORCH_ALLOW.search("review diff") is not None
    assert ost.ORCH_ALLOW.search("git worktree list") is not None
    assert ost.ORCH_ALLOW.search("gh pr checks 5") is None   # gh dropped from the allow-list
    assert ost.ORCH_ALLOW.search("gh ship 5") is None
    assert ost.ORCH_ALLOW.search("curl -X POST http://h/api") is None
    assert ost.ORCH_ALLOW.search("ssh root@h 'df -h'") is None
    impl = ost._is_implementation_bash
    assert impl("tg 'x' | tail") is False
    assert impl("gh pr checks 5 | grep fail | head") is True   # gh is delegated now
    assert impl("git log | grep x | head") is False  # plain read-only path, no tg/gh needed
    # a chain fronted by a non-sanctioned head is judged on its full content — a 3-segment curl
    # chain is impl-shaped, and a bare-`&` push behind a tg report is caught by the `&` split.
    assert impl("curl -X POST http://h/api | tg done | tail") is True
    assert impl("tg done & git push origin main; ls") is True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_double_quoted_substitution_is_live_and_judged(tmp_path, monkeypatch):
    """Quote SEMANTICS (#164 review P2): `$(…)`/backticks EXECUTE inside DOUBLE quotes, so a
    double-quoted mutating substitution must be extracted and judged (warn-then-block); a
    SINGLE-quoted one is literal text and stays allowed."""
    assert ost._is_implementation_bash('gh ship "$(gh api repos/o/r/issues -X POST -f title=x)"') is True
    assert ost._is_implementation_bash('gh ship 605 --note "$(git push origin main)" | tail -3') is True
    assert ost._is_implementation_bash('tg "`git commit -m x`"') is True
    assert ost._is_implementation_bash("tg 'saw $(git push) in logs'") is False
    # a gh head is delegated regardless of what its substitution contains (tg#7103)
    assert ost._is_implementation_bash('gh pr view "$(gh pr list --json number -q .n)"') is True
    # a still-sanctioned `tg` head with a read-only substitution stays allowed in either quote form
    assert ost._is_implementation_bash('tg "$(git log --oneline -1)"') is False
    assert ost._is_implementation_bash("tg 'literal $(git push) text'") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": 'gh ship "$(gh api repos/o/r/issues -X POST -f title=x)"'}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── Inline command hatch (pre-bash) + write-target config routing (pre-write) ────────────


def test_pre_bash_inline_command_triggers_hatch(tmp_path, monkeypatch):
    """A pre-bash repeat offense with the justification supplied as an inline command prefix
    (env var NOT exported) must reach tg-ctl — the hook now passes the command string through so
    request_hatch_approval can parse the leading assignment. Regression guard for the inline form
    being unusable (Codex P2 on #232)."""
    marker = tmp_path / "asked"
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'touch {marker}\nprintf "%s" "$2" > "{question}"\nprintf approved\nexit 0\n',
    )
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    impl = "sed -i 's/a/b/' f && npm run build && echo done"
    inline = 'RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="one generated file, no subagent" ' + impl
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": inline}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")  # repeat → inline hatch → approve
    assert c == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "one generated file, no subagent" in question.read_text()


def test_pre_write_hatch_ignores_target_repo_tg_ctl_path(tmp_path, monkeypatch):
    """SECURITY (P1): for a pre-write into ANOTHER repo, the approval binary (tg-ctl) must come
    from the account home, NEVER the target (or shell) repo's rig.yaml. Otherwise a guarded agent
    commits `rig.yaml` with `tg_ctl_path: /tmp/always-exit-0` into the repo it writes and
    self-approves. Here the target repo's rig.yaml points tg_ctl_path at an always-exit-0 binary,
    home carries no override, and the trusted fallback is emptied — so the request must DENY and
    the repo-local binary must never run. (The orchestrator still resolves the *enablement* knob
    `orchestrator_only` from the target repo via cfg_dir — that lookup is in the hook, not the
    approval-binary lookup, which is now home-anchored in the shared lib.)"""
    marker = tmp_path / "evil-called"
    evil = _fake_tg_ctl(tmp_path / "evil-tg-ctl", f"touch {marker}\nexit 0\n")  # would approve
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", ())
    clean_home = tmp_path / "home"
    clean_home.mkdir()  # account home carries NO tg_ctl_path override
    monkeypatch.setattr(ost.hatch_escalation, "resolve_home", lambda: str(clean_home))
    target_repo = tmp_path / "target"
    (target_repo / "src").mkdir(parents=True)
    (target_repo / "rig.yaml").write_text(f'agent_hooks:\n  tg_ctl_path: "{evil}"\n')
    event = {
        "point": "pre-write",
        "cwd": str(target_repo),
        "args": {"file_path": str(target_repo / "src" / "a.ts")},
    }
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN": "attacker-supplied justification"},
    )
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()  # the repo-local (attacker) binary was NEVER executed
