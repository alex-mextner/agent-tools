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
import subprocess
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


# ── agent-tools#307: the ONE provably-safe heredoc shape is carved out ──────────────────────
# `$(cat <<'DELIM' ...body... DELIM)` is inert: a QUOTED delimiter guarantees zero expansion in
# the body, and `cat` only echoes stdin — so the whole substitution can only ever evaluate to a
# plain string. This is the standard idiom for a multi-line/HTML `tg` report and used to trip the
# blanket HEREDOC block even though `tg` alone is sanctioned orchestration (ORCH_ALLOW).

@pytest.mark.parametrize("command", [
    # the exact real command that used to fail (Alex tg#307)
    "tg --format html --tag report --title \"some title\" \"$(cat <<'EOF'\n<b>line one</b>\n"
    "line two\nEOF\n)\"",
    # plain report body, delimiter alone on its own line, closing paren on the next line
    "tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    # `<<-` dash variant with tab-indented body/terminator
    "tg \"$(cat <<-'EOF'\n\tbody\n\tEOF\n)\"",
    # double-quoted delimiter (still quoted -> still inert)
    'tg "$(cat <<"EOF"\nbody\nEOF\n)"',
    # closing paren on the SAME line as the terminator (verified real-bash syntax)
    "tg \"$(cat <<'EOF'\nbody\nEOF)\"",
    # two separate instances of the idiom chained with && must both collapse
    "tg \"$(cat <<'EOF'\na\nEOF\n)\" && tg \"$(cat <<'EOF2'\nb\nEOF2\n)\"",
    # the idiom used for two separate arguments of the SAME call
    "tg --title \"$(cat <<'T'\nTitle\nT\n)\" \"$(cat <<'B'\nBody\nB\n)\"",
    # chained after other sanctioned orchestration
    "review diff && tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    # a body line that only PARTIALLY matches the delimiter must not be mistaken for the terminator
    "tg \"$(cat <<'EOF'\nEOFxyz\nEOF\n)\"",
])
def test_safe_heredoc_cat_substitution_allows(command, tmp_path, monkeypatch):
    """The exact `$(cat <<'DELIM' ...DELIM...)` shape is provably inert and must never warn OR
    block, however it is chained or repeated at depth zero (agent-tools#307; NOT nested — nesting
    is explicitly rejected, see test_safe_heredoc_carveout_never_fires_for_a_reexecuting_consumer)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    assert "message" not in json.loads(out1), command  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


@pytest.mark.parametrize("command", [
    # `git commit` is never orchestrator-allowed, heredoc or not
    "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"",
    # an UNQUOTED delimiter allows live expansion in the body -> not the safe shape
    "tg \"$(cat <<EOF\n$(git commit -m oops)\nEOF\n)\"",
    # a chained mutation AFTER the safe idiom still blocks on its own segment
    "tg \"$(cat <<'EOF'\nbody\nEOF\n)\" && git commit -m x",
    # ...even when the safe idiom uses the same-line closing-paren form
    "tg \"$(cat <<'EOF'\nbody\nEOF)\" && git commit -m x",
    # a bare `&` background smuggles a push behind an otherwise-safe heredoc report
    "tg \"$(cat <<'EOF'\nbody\nEOF\n)\" & git push",
    # a terminator line with trailing whitespace is NOT a real bash terminator (verified against
    # real bash) -> the heredoc never closes -> stays the ordinary (blocked) blanket shape
    "tg \"$(cat <<'EOF'\nEOF \n)\"",
    # `gh` is delegated regardless of what rides alongside it (tg#7103)
    "gh ship 605 && tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
])
def test_safe_heredoc_carveout_does_not_launder_other_implementation(command, tmp_path, monkeypatch):
    """The narrow carve-out only neutralizes the ONE exact shape — it must never launder a
    mutation elsewhere on the line, an unquoted delimiter, or a heredoc that never actually
    terminates (agent-tools#307)."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_bare_heredoc_to_file_still_blocks_not_the_safe_shape(tmp_path, monkeypatch):
    """A bare (non-substitution) heredoc redirected to a file is not `$(cat <<'DELIM' ...)` at
    all — it never matches the carve-out and still hits the ordinary blanket block."""
    command = "cat <<'EOF' > /tmp/evil.py\nprint(1)\nEOF"
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # untouched
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_safe_heredoc_body_content_is_irrelevant_once_collapsed():
    """A quoted-delimiter heredoc body that superficially LOOKS like a mutation (`$(git commit)`,
    `` `git push` ``, `rm -rf /`) is still 100% literal text — the whole span collapses to `$()`
    regardless of what the body contains, and the command remains allowed (the safety argument
    agent-tools#307 relies on: a quoted delimiter guarantees no expansion happens, ever)."""
    command = ("tg \"$(cat <<'EOF'\n$(git commit -m x) && rm -rf / ; `git push`\nEOF\n)\"")
    assert ost._strip_safe_heredoc_cat_substitutions(command) == 'tg "$()"'
    assert ost._is_implementation_bash(command) is False


def test_safe_heredoc_strip_is_a_noop_without_the_exact_shape():
    """Pin the boundary directly: only the exact `$(cat <<'DELIM' ...DELIM...)` shape is
    rewritten; anything else — unquoted delimiter, non-`cat` consumer, no substitution wrapper —
    passes through `_strip_safe_heredoc_cat_substitutions` completely unchanged."""
    strip = ost._strip_safe_heredoc_cat_substitutions
    assert strip("tg 'plain msg'") == "tg 'plain msg'"
    unquoted = "tg \"$(cat <<EOF\nbody\nEOF\n)\""
    assert strip(unquoted) == unquoted  # unquoted delimiter -> untouched
    non_cat = "tg \"$(tac <<'EOF'\nbody\nEOF\n)\""
    assert strip(non_cat) == non_cat  # not `cat` -> untouched
    bare = "cat <<'EOF' > f\nbody\nEOF"
    assert strip(bare) == bare  # not wrapped in $(...) -> untouched


# ── agent-tools#307 review (Codex P1): the safe SHAPE alone is not sufficient — WHAT consumes
# the resulting plain string matters too. `eval`/`bash -c`/a nested `$(...)` all RE-EXECUTE that
# string, so the carve-out must never fire unless the span is a plain, double-quoted argument of
# an already-`tg`-headed segment at substitution depth 0. Verified against real bash that
# the nested-substitution case is a genuine, not hypothetical, code-execution path: running
# `x=$($(cat <<'EOF'\ntouch /tmp/marker\nEOF\n))` in a real shell does create the file.

@pytest.mark.parametrize("command", [
    # `eval` re-executes its argument as a new command
    "eval \"$(cat <<'EOF'\nrm -rf /\nEOF\n)\"",
    # `bash -c` / `sh -c` / `source` are the same class of re-execution
    "bash -c \"$(cat <<'EOF'\nrm -rf /\nEOF\n)\"",
    "sh -c \"$(cat <<'EOF'\nrm -rf /\nEOF\n)\"",
    "source \"$(cat <<'EOF'\nrm -rf /\nEOF\n)\"",
    # a nested `$($(cat <<'EOF' ...)) ` — the OUTER substitution runs the inner's plain-string
    # result as a BRAND NEW command (real-bash-verified, not just theoretical)
    "tg \"$($(cat <<'EOF'\ntouch /tmp/pwned\nEOF\n))\"",
    # redirecting the (safe-shaped) substitution's value to a file is still a file write
    "printf \"%s\" \"$(cat <<'EOF'\nbad content\nEOF\n)\" > /tmp/generated.py",
    # a plain, non-orchestration head is never eligible regardless of shape
    "python3 -c \"$(cat <<'EOF'\nimport os; os.system('rm -rf /')\nEOF\n)\"",
])
def test_safe_heredoc_carveout_never_fires_for_a_reexecuting_consumer(command, tmp_path, monkeypatch):
    """The exact `$(cat <<'DELIM' ...)` SHAPE is necessary but not sufficient — the carve-out only
    ever collapses when the span is ALSO a plain, double-quoted argument of an already-sanctioned
    (`tg`) segment at substitution depth 0 (agent-tools#307, Codex P1). Every one of these commands has
    the safe shape but an unsafe CONSUMER, so the span must stay uncollapsed and the ordinary
    blanket HEREDOC rule must still fire."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command  # untouched
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_nested_command_substitution_really_does_reexecute_in_real_bash(tmp_path):
    """Documents WHY the nesting check exists, with a live proof, not just an assertion about our
    own classifier: a bare `$($(cat <<'EOF' ...))` really does run the inner's plain-string output
    as a brand-new shell command (verified 2026-07-19). If bash ever stopped doing this, the
    nesting restriction would be over-cautious rather than load-bearing — but today it is not."""
    marker = tmp_path / "nested_subst_proof_marker"
    script = f"x=$($(cat <<'EOF'\ntouch {marker}\nEOF\n))\necho done"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert marker.exists(), "nested $($(cat<<'EOF'...)) did not execute — assumption is stale"


# ── agent-tools#307 review round 2 (Codex P1): three MORE ways a safe-SHAPED heredoc could be
# fed to a dangerous consumer or hidden from the classifier entirely — each verified executing for
# real in bash, not just reasoned about, before being fixed.

@pytest.mark.parametrize("command", [
    "tg > \"$(cat <<'EOF'\n/tmp/evil-target\nEOF\n)\"",
    "tg >> \"$(cat <<'EOF'\n/tmp/evil-target\nEOF\n)\"",
    "tg < \"$(cat <<'EOF'\n/tmp/evil-source\nEOF\n)\"",
    "tg 2> \"$(cat <<'EOF'\n/tmp/evil-target\nEOF\n)\"",
    "tg > $(cat <<'EOF'\n/tmp/evil-target\nEOF\n)",  # unquoted redirect target
])
def test_heredoc_as_redirect_target_still_blocks(command, tmp_path, monkeypatch):
    """A safe-shaped heredoc used as a REDIRECT TARGET (`tg > "$(cat <<'EOF' ...)"`) is a
    fundamentally different consumption than a plain argument: the substitution supplies a
    FILENAME, and tg's own stdout gets written there — an attacker who controls the heredoc body
    controls the write path. Must never collapse (agent-tools#307 review round 2, Codex P1)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


# ── agent-tools#307 review round 3: three MORE bypasses, each live-verified before fixing ────

@pytest.mark.parametrize("command", [
    # `>&`/`>|`/`<&` all contain a `>`/`<` but not as the LAST character before the match — the
    # old `_REDIRECT_BEFORE` regex ("last char is `<`/`>`") missed these (Opus Finding 1, verified:
    # `tg >& "$(cat <<'EOF' ...)"` really does redirect tg's stdout+stderr to the target path)
    "tg >& \"$(cat <<'EOF'\n/tmp/evil-target\nEOF\n)\"",
    "tg >| \"$(cat <<'EOF'\n/tmp/evil-target\nEOF\n)\"",
    # a word-concatenated target: no whitespace between a literal path prefix and the
    # substitution means real bash treats them as ONE combined redirect-target word (Codex P1)
    "tg > path/prefix/\"$(cat <<'EOF'\n../../etc/evil\nEOF\n)\"",
])
def test_redirect_target_forms_missed_by_old_regex_still_block(command, tmp_path, monkeypatch):
    """`_scan_segment_signals` scans the WHOLE segment for any bare `<`/`>` rather than
    just checking the character immediately before the match — this catches `>&`/`>|`/zsh `>!`
    and word-concatenated targets that a narrower "last char" check would miss (agent-tools#307
    review round 3, Opus Finding 1 + Codex P1)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_quote_inside_earlier_heredoc_body_does_not_widen_head_match(tmp_path, monkeypatch):
    """Live proof + regression: a literal, unpaired `"` inside an EARLIER heredoc's own body used
    to desync the (then-single, global) quote tracker, making a LATER `eval` segment look like it
    was still part of the earlier `tg`-headed one — laundering the whole command to ALLOWED.
    `_mask_at_top_level` now tracks quote state PER SUBSTITUTION NESTING LEVEL (each is bash's own
    independent quote scope), which fixes this at the root (agent-tools#307 review round 3, Opus
    Finding 2 / Codex P1; the per-level design was further hardened in round 7 to also catch a
    QUOTED `)`/`(` inside a nested scope — see test_quoted_paren_inside_nested_substitution_
    desyncs_depth_still_blocks). The first (legitimately `tg`-headed, non-nested)
    heredoc still collapses on its own merits — collapsing a SAFE span is never the problem — but
    the second (`eval`-headed) one must not, and the overall command must still BLOCK. Note bash
    ITSELF chokes on an odd quote count in a bare heredoc body embedded in `$(...)` (a syntax
    error, nothing executes) — proven here with `;`-separated commands instead, which bash parses
    and runs fine."""
    marker = tmp_path / "quote_body_desync_marker"
    command = f'tg "$(cat <<\'A\'\n"\nA\n)" ; eval "$(cat <<\'B\'\ntouch {marker}\nB\n)"'
    stripped = ost._strip_safe_heredoc_cat_substitutions(command)
    assert "$()" in stripped  # the first, legitimately-safe heredoc DOES collapse
    assert "<<'B'" in stripped  # the second (eval-headed) one stays raw, uncollapsed
    assert ost._is_implementation_bash(command) is True  # overall: still BLOCKED
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_escaped_paren_inside_nested_eval_still_blocks(tmp_path):
    """Live proof + regression: a backslash-escaped `)` inside a double-quoted argument of a
    NESTED `eval` defeats naive bracket-counting (the `)` is just literal text inside `"..."`, not
    a real substitution close) — verified against real bash that a smuggled command really
    executes via this path. `_scan_segment_signals` closes this, and the whole class of
    escape-based bypasses, by rejecting outright whenever ANY backslash appears in the segment
    (agent-tools#307 review round 3, Codex P1)."""
    marker = tmp_path / "escaped_paren_marker"
    script = (
        'tg_stub() { :; }\n'
        f'tg_stub "$(eval "x=\\)" $(cat <<\'EOF\'\ntouch {marker}\nEOF\n))"'
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "escaped ) inside nested eval did not re-execute the smuggled command"

    command = 'tg "$(eval "x=\\)" $(cat <<\'EOF\'\ntouch /tmp/evil\nEOF\n))"'
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True


def test_ansi_c_quote_hiding_a_chain_operator_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (review round 4, Opus): ANSI-C `$'...'` quoting is a DIFFERENT
    quoting mode than plain `'...'` — inside it, `\\'` is a literal escaped quote that does NOT
    end the string (`$'a\\'b'` really is the 3-character value `a'b`), but `_mask_at_top_level`
    doesn't know that and closes the span at the `\\'` anyway, exposing whatever text follows (a
    `;`/`tg` word) as if it were live top-level structure — even though bash treats the whole
    `$'...'` as one opaque literal argument to `eval`. Fixed by treating ANY backslash or `$'`
    sequence anywhere in the segment as disqualifying, and (critically) NEVER resetting that flag
    on a segment boundary, since the boundary detection itself can be the thing corrupted by this
    exact signal."""
    marker = tmp_path / "ansi_c_quote_marker"
    script = (
        'tg_stub() { echo "tg_stub ran: $*"; }\n'
        f"eval $'\\'; tg_stub' \"$(cat <<'EOF'\ntouch {marker}\nEOF\n)\""
    )
    # NOTE (agent-tools#307 review round 9, Codex P3: a prior version ran this exact subprocess and
    # asserted NOTHING about its outcome — dead weight, not a real proof of anything): this
    # construction is a real-bash SYNTAX ERROR once `eval` re-parses the unbalanced-looking quote,
    # so there is no marker-file execution to prove here. What IS worth pinning, and now is: that
    # bash really does reject it (a nonzero exit + the marker never appearing) rather than silently
    # doing something else — the classifier assertions below are the actual regression pin.
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0, (
        f"expected a real-bash syntax error from the unbalanced eval re-parse, got exit "
        f"{result.returncode} (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert not marker.exists()

    command = "eval $'\\'; tg' \"$(cat <<'EOF'\ntouch /tmp/evil\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_subshell_grouping_paren_desyncs_depth_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (review round 6, Opus): a bare `(` opening a subshell or
    arithmetic grouping (`(...)`/`$((...))`) used to not be counted as a depth-OPENER by our
    bracket counter — only `$(`/backtick/`<(`/`>(` were — but its matching `)` WAS counted as a
    depth-CLOSER, so a balanced `(...)` inside a `$(...)` silently dropped the counter by one,
    making a genuinely-nested heredoc look like it sits at depth 0. Verified against real bash
    that this is exploitable: the `;` after the subshell makes the inner `$(cat <<'EOF' ...)` a
    standalone command whose plain-string result gets executed. Originally fixed (round 6) by
    poisoning on any bare `(`; round 7 replaced that with correct per-nesting-level tracking
    (`_mask_step` pushes a stack level for a bare `(` too), which also fixes this AND stops
    over-blocking a legitimate parenthetical remark elsewhere — see
    test_legitimate_parenthetical_in_double_quoted_title_does_not_desync below."""
    marker = tmp_path / "subshell_desync_marker"
    script = (
        'tg_stub() { echo "tg_stub ran: $*"; }\n'
        f"tg_stub \"$( (:) ; $(cat <<'EOF'\ntouch {marker}\nEOF\n) )\""
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "subshell-desync trick did not re-execute the smuggled command"

    command = "tg \"$( (:) ; $(cat <<'EOF'\ntouch /tmp/evil\nEOF\n) )\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_quoted_paren_inside_nested_substitution_desyncs_depth_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (review round 7, Opus Finding 1): a `)` that is single-quoted,
    LITERAL text inside a nested `$(...)` was wrongly counted as a real closing paren by the
    round-3-6 "ignore quotes entirely while nested" design, silently returning `depth` to 0 early
    and letting a FURTHER-nested, re-executing heredoc collapse and get allowed. Verified against
    real bash that this is exploitable: `echo ')'` prints a literal `)` (quoted, inert), `;`
    separates it from a command-position `$(cat <<'EOF' ...)` whose plain-string result then gets
    executed. Fixed by tracking quote state PER SUBSTITUTION NESTING LEVEL (`_mask_step`'s stack)
    instead of suspending it entirely while nested — the quoted `)` is now correctly recognized
    as literal (blanked) at its own level, so it never desyncs the depth count."""
    marker = tmp_path / "quoted_paren_desync_marker"
    script = (
        'tg_stub() { echo "tg_stub ran: $*"; }\n'
        f"tg_stub \"$(echo ')' ; $(cat <<'EOF'\ntouch {marker}\nEOF\n))\""
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "quoted-paren-desync trick did not re-execute the smuggled command"

    command = "tg \"$(echo ')' ; $(cat <<'EOF'\ntouch /tmp/evil\nEOF\n))\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_legitimate_parenthetical_in_double_quoted_title_does_not_desync(tmp_path, monkeypatch):
    """Round 7's per-nesting-level quote tracking is not just a security fix but a precision
    improvement: a literal `(`/`)` inside an EARLIER double-quoted argument (an ordinary
    parenthetical remark in a report title) is correctly recognized as inert quoted text and
    blanked before it can desync depth or reach the poison checks — so a completely benign
    `tg "note (see below)" "$(cat <<'EOF' ...)"` still collapses and is allowed, instead of
    being over-blocked the way a cruder "poison on any bare (" rule would have."""
    command = "tg \"note (see below)\" \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == 'tg "note (see below)" "$()"'
    assert ost._is_implementation_bash(command) is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    assert "message" not in json.loads(out1)
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


def test_single_quote_non_nesting_bypass_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (review round 8, Opus/Codex P1): bash single quotes do NOT nest
    and have NO escape mechanism — `tg 'foo $(cat <<'EOF' ... EOF)'` looks like one big quoted
    argument but is really an alternating series of quoted/UNQUOTED spans, because each `'`
    toggles the SAME quote regardless of what precedes it. This hides a genuinely LIVE, unquoted
    `; echo PWNED ;` in the middle — verified against real bash that it executes. The classifier
    used to fall back to a raw, un-stripped regex match (`_HEREDOC_SAFE_CONSUMER.search(head)`)
    whenever `shlex` could not parse the (genuinely ambiguous) head, which still saw a leading
    `tg` and allowed it. Fixed by rejecting outright whenever shlex can't parse the head even
    after the standard one-dangling-quote recovery, instead of falling back to a naive regex."""
    marker = tmp_path / "single_quote_bypass_marker"
    script = (
        'tg_stub() { :; }\n'
        f"tg_stub 'foo $(cat <<'EOF'\n' ; echo REALLY_RAN ; touch {marker} ; x='\nEOF\n)'"
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "single-quote non-nesting bypass did not re-execute the smuggled command"

    command = "tg 'foo $(cat <<'EOF'\n' ; echo PWNED ; x='\nEOF\n)'"
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_word_concatenated_command_name_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (review round 8, Codex P1): with NO whitespace between the
    consumer command and the heredoc substitution (`tg$(cat <<'EOF' ...)`), bash concatenates the
    substitution's VALUE directly onto the command name, forming a DIFFERENT executable (`tg-ctl`
    if the body is `-ctl`) — verified against real bash by placing a fake `tg-ctl` binary on
    PATH. The old consumer check only verified the text BEFORE the match started with `tg`/
    `review`, never that the match itself begins a genuinely separate word. Fixed by requiring
    the character immediately before the match to be whitespace or an opening quote."""
    marker = tmp_path / "concat_marker"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_tg_ctl = fakebin / "tg-ctl"
    fake_tg_ctl.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake_tg_ctl.chmod(0o755)
    script = (
        f'export PATH="{fakebin}:$PATH"\n'
        "tg$(cat <<'EOF'\n-ctl\nEOF\n)"
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "word-concatenated command name did not resolve to the fake tg-ctl"

    command = "tg$(cat <<'EOF'\n-ctl\nEOF\n)"
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_quote_concatenated_command_name_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (agent-tools#307 review round 9, Codex P1): `tg"$(cat <<'EOF'
    ...)"`  —  an opening double-quote glued DIRECTLY onto `tg` with NO space — is a DIFFERENT
    word-concatenation shape than the plain `tg$(...)` case above: here the character right before
    the substitution IS a quote, and a prior fix accepted ANY quote character in that position as
    proof of a fresh word, without checking what comes before the quote itself. That cannot tell
    apart the safe `tg "$(...)"` (space, then quote) from this unsafe `tg"$(...)"` (no space at
    all) — both have a quote at `pos - 1`. Verified against real bash by placing a fake `tg-ctl`
    binary on PATH: with a `-ctl` body, `tg"$(cat <<'EOF'\n-ctl\nEOF\n)"` really invokes it, exactly
    like the no-quote case. Fixed by `_starts_separate_word` looking one character further back,
    past the quote, for the whitespace that actually matters."""
    marker = tmp_path / "quote_concat_marker"
    fakebin = tmp_path / "fakebin_qc"
    fakebin.mkdir()
    fake_tg_ctl = fakebin / "tg-ctl"
    fake_tg_ctl.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake_tg_ctl.chmod(0o755)
    script = (
        f'export PATH="{fakebin}:$PATH"\n'
        "tg\"$(cat <<'EOF'\n-ctl\nEOF\n)\""
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "quote-concatenated command name did not resolve to the fake tg-ctl"

    command = "tg\"$(cat <<'EOF'\n-ctl\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_exact_token_consumer_match_rejects_different_executable(tmp_path, monkeypatch):
    """Live proof + regression (agent-tools#307 review round 9, Codex P1): the consumer-head check
    used a bare `\\b` word boundary (`tg\\b`), but `\\b` only tests a word/non-word character
    TRANSITION, not "end of token" — it is perfectly satisfied right before the `-` in `tg-ctl`,
    so a heredoc fed to the genuinely DIFFERENT command `tg-ctl` was wrongly treated as the vetted
    `tg`. Verified against real bash by placing a fake `tg-ctl` binary on PATH: `tg-ctl
    "$(cat <<'EOF'\\nfoo\\nEOF\\n)"` really invokes it. Fixed with an exact-token lookahead
    (`tg(?=\\s|$)`), the same style `CD_HEAD` already uses elsewhere in this file."""
    marker = tmp_path / "exact_token_marker"
    fakebin = tmp_path / "fakebin_et"
    fakebin.mkdir()
    fake_tg_ctl = fakebin / "tg-ctl"
    fake_tg_ctl.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake_tg_ctl.chmod(0o755)
    script = (
        f'export PATH="{fakebin}:$PATH"\n'
        "tg-ctl \"$(cat <<'EOF'\nfoo\nEOF\n)\""
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "tg-ctl (a different executable) did not actually run"

    command = "tg-ctl \"$(cat <<'EOF'\nfoo\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_open_top_level_single_quote_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (agent-tools#307 review round 9, Codex P1): a MINIMAL single-quote
    non-nesting bypass, narrower than the round-8 fix's own regression — no `foo ` prefix needed.
    `tg '$(cat <<'EOF'\\n' ; printf LIVE_PAYLOAD ; x='\\nEOF\\n)'` opens a single-quote right after
    `tg `, closes and reopens it around a genuinely LIVE, unquoted `; printf LIVE_PAYLOAD ; x=`
    (bash single quotes do not nest), so the `$(cat <<'EOF'` shape the regex matched was NEVER a
    real substitution start at all from bash's point of view — it sits inside an open top-level
    single-quote, where `$(` is just literal text. `_mask_at_top_level` already tracked `in_single`
    per nesting level internally but never surfaced it to the caller, so this minimal shape (unlike
    the round-8 regression, which happened to also fail the head-parse recovery) slipped through
    even after that fix. Verified against real bash that `printf LIVE_PAYLOAD` really runs. Fixed
    by surfacing `in_single` from `_mask_at_top_level` and rejecting whenever it is set."""
    marker = tmp_path / "open_single_quote_marker"
    script = (
        'tg_stub() { :; }\n'
        f"tg_stub '$(cat <<'EOF'\n' ; touch {marker} ; x='\nEOF\n)'"
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "the minimal open-single-quote bypass did not re-execute the smuggled command"

    command = "tg '$(cat <<'EOF'\n' ; touch /tmp/evil ; x='\nEOF\n)'"
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_unquoted_substitution_word_splitting_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression (agent-tools#307 review round 10, Codex P1): the safety argument
    ("the substitution can only ever be a plain string") silently assumed the result reaches `tg`
    as exactly ONE argument — true only when the substitution is wrapped in double quotes, which
    every legitimate example of this idiom does (`tg "$(cat <<'EOF' ...)"`). An UNQUOTED
    substitution (`tg $(cat <<'EOF' ...)`, no surrounding quotes) is subject to bash's ordinary
    word-splitting (on IFS) and filename globbing: a heredoc body of `--file /etc/passwd` really
    becomes the TWO separate argv elements `--file` and `/etc/passwd` — letting the heredoc body
    inject arbitrary CLI FLAGS into `tg`'s own invocation (verified below with a stub receiving
    `"$@"`), not just a literal message string. Fixed by requiring the candidate position to sit
    INSIDE an open top-level double-quote (`_mask_at_top_level`'s `in_double`), which is what the
    documented idiom actually relies on for the "exactly one argument" guarantee."""
    log = tmp_path / "argv_log"
    script = (
        f'tg_stub() {{ for a in "$@"; do echo "ARG:[$a]" >> {log}; done; }}\n'
        "tg_stub $(cat <<'EOF'\n--file /etc/passwd\nEOF\n)"
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    argv = log.read_text().splitlines() if log.exists() else []
    assert argv == ["ARG:[--file]", "ARG:[/etc/passwd]"], (
        f"expected bash to word-split the unquoted substitution into two argv elements, got {argv!r}"
    )

    command = "tg $(cat <<'EOF'\n--file /etc/passwd\nEOF\n)"
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_comment_suppresses_a_real_heredoc_in_real_bash(tmp_path):
    """Live proof (verified 2026-07-19): a `<<'EOF'` written after a `#` on the same line is NOT a
    real heredoc at all in bash — the comment suppresses it, so the following line is an ordinary,
    LIVE, separate command, not heredoc body. This is WHY a comment-hidden pseudo-heredoc must
    never be collapsed by the carve-out (agent-tools#307 review round 2, Codex P1)."""
    marker = tmp_path / "comment_hidden_proof_marker"
    script = f"tg_stub() {{ :; }}\ntg_stub ok # $(cat <<'EOF'\ntouch {marker}\nEOF\n)"
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "a #-hidden <<'EOF' failed to let the next line execute for real"


@pytest.mark.parametrize("command", [
    # the `$(cat <<'EOF'` sits after a `#` on `tg`'s own line -> not a real heredoc; the body line
    # is a real, separate, live command that must be judged, not silently erased by the collapse
    "tg ok # $(cat <<'EOF'\ntouch /tmp/evil\nEOF\n)",
    # same idea, but the fake heredoc is hidden inside a nested LIVE substitution's own comment
    "tg \"$(echo x # $(cat <<'EOF'\ntouch /tmp/evil\nEOF\n)\"",
])
def test_comment_hidden_pseudo_heredoc_still_blocks(command, tmp_path, monkeypatch):
    """A `#` before `$(cat <<'DELIM'` makes the WHOLE thing inert comment text in real bash — the
    apparent "heredoc body" that follows is actually live, executing shell (agent-tools#307 review
    round 2, Codex P1; see test_comment_suppresses_a_real_heredoc_in_real_bash for the live proof).
    The carve-out must never collapse (and thus erase) this span."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_unbalanced_substitution_opener_inside_a_comment_does_not_leak_depth(tmp_path, monkeypatch):
    """Regression (agent-tools#307 review round 11, Opus): a prior `_mask_step` ordering ran the
    `$(`/backtick/`<(`/`>(` push checks BEFORE the `in_comment` check, so a substitution opener
    sitting inside a real `#` comment still pushed a nesting level — a push that could never
    balance (the `\\n` ending the comment is consumed at the pushed level, not popped), permanently
    inflating depth and over-blocking a LATER, otherwise-safe heredoc in the same multi-line
    command. `tg foo # $( unbalanced note` is pure comment text in real bash (verified: it never
    creates a marker file even though the line LOOKS like it opens a live substitution), so the
    SECOND line's ordinary `tg "$(cat <<'EOF' ...)"` must still collapse and be allowed."""
    marker = tmp_path / "comment_unbalanced_marker"
    script = f"tg_stub() {{ :; }}\ntg_stub foo # $( touch {marker}\ntg_stub bar\n"
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert not marker.exists(), "a # comment containing an unbalanced $( unexpectedly ran live code"

    command = "tg foo # $( unbalanced note\ntg \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert "$()" in ost._strip_safe_heredoc_cat_substitutions(command)
    assert ost._is_implementation_bash(command) is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    assert "message" not in json.loads(out1)
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


def test_process_substitution_inside_double_quotes_is_literal_not_a_push(tmp_path, monkeypatch):
    """Regression (agent-tools#307 review round 11, Opus): bash treats `<(`/`>(` process
    substitution as PLAIN LITERAL TEXT when it appears inside double quotes (verified: `echo
    "<(echo hi)"` prints the literal text, never expanding it — unlike `$(...)`, which still
    executes inside double quotes either way). A prior `_mask_step` pushed a nesting level for
    `<(`/`>(` unconditionally, regardless of quoting, so a harmless `tg "note <(fake)" "$(cat
    <<'EOF' ...)"` was wrongly over-blocked (never popped, since there's no real subshell to close
    it). Fixed by only pushing when NOT already inside a double-quoted span."""
    marker = tmp_path / "process_subst_marker"
    script = f'tg_stub() {{ :; }}\ntg_stub "note <(touch {marker})"\n'
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert not marker.exists(), "<(...) inside double quotes unexpectedly ran as process substitution"

    command = "tg \"note <(fake)\" \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert "$()" in ost._strip_safe_heredoc_cat_substitutions(command)
    assert ost._is_implementation_bash(command) is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    assert "message" not in json.loads(out1)
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


def test_backslash_continuation_smuggles_eval_still_blocks(tmp_path, monkeypatch):
    """Live proof + regression: a backslash-newline is a real bash LINE CONTINUATION (removed
    before parsing), so `eval \\` + newline + ` tg "$(cat <<'EOF' ...)"` is ONE `eval` invocation —
    not a fresh `tg`-headed segment starting after the newline. Verified against real bash first
    (a smuggled command inside the heredoc body really executes via eval's re-parse), then pinned
    against the classifier (agent-tools#307 review round 2, Codex P1)."""
    marker = tmp_path / "continuation_proof_marker"
    script = f"tg_stub() {{ :; }}\neval \\\n tg_stub \"$(cat <<'EOF'\n; touch {marker}\nEOF\n)\""
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert marker.exists(), "backslash-continued eval did not re-execute the smuggled command"

    command = "eval \\\n tg \"$(cat <<'EOF'\n; touch /tmp/evil\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    # Codex review round 2, P2: wrapper-stripping must apply to the heredoc carve-out too, so it
    # is never MORE restrictive than the equivalent non-heredoc wrapped call already allowed
    # elsewhere in this file (`_strip_wrappers` covers env/timeout/etc. for every other segment).
    "env REPORT=1 tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    "REPORT=1 tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    "timeout 60 tg \"$(cat <<'EOF'\nbody\nEOF\n)\"",
])
def test_wrapper_prefixed_heredoc_calls_still_collapse(command, tmp_path, monkeypatch):
    """`env`/`timeout`/a leading `VAR=val` assignment must not defeat the carve-out — the same
    wrapper-stripping `_strip_wrappers` already applies to every other segment in this file now
    also applies to the heredoc consumer-head check (agent-tools#307 review round 2, Codex P2)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    assert "message" not in json.loads(out1), command  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow", command


def test_review_is_no_longer_a_heredoc_consumer(tmp_path, monkeypatch):
    """`review` was DROPPED from the heredoc carve-out's own consumer set in agent-tools#307
    review round 9 (Codex P2): in the STANDARD installed configuration (both hooks present, no
    hatch-approval override in play), the sibling `no-long-inline-process` hook already intercepts
    an orchestrator-run `review …` at a LOWER priority number (35) than this hook's (45) — but that
    is NOT an absolute guarantee (agent-tools#307 review round 10, Codex P2): the hook-bridge
    continues to LATER descriptors whenever an earlier one ALLOWS (fail-open, a hatch-approved
    exception, or the sibling hook simply not being installed as an independently-installable
    catalog item), and a dispatched subagent is exempt from THIS hook regardless. So `review`
    collapsing a heredoc never mattered in the common case; `review` (still sanctioned
    orchestration via `ORCH_ALLOW` on its own, heredoc or not) must now still hit the ordinary
    blanket block when fed one here, exactly like `git worktree list` — this pins THIS hook's own
    behavior, independent of what any other installed hook may or may not do.

    Uses `timeout` (not `gtimeout`) as the wrapper: `no-long-inline-process`'s own wrapper table
    does not recognize `gtimeout` (agent-tools#307 review round 10, Codex P2), so a `gtimeout`-
    wrapped example would not actually demonstrate the cross-hook interception this docstring
    describes even in the common case."""
    command = "timeout 60 review diff \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert ost.ORCH_ALLOW.search("review diff") is not None  # sanctioned orchestration...
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # ...but not a consumer
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_git_worktree_list_is_not_a_heredoc_consumer(tmp_path, monkeypatch):
    """`git worktree list` is sanctioned orchestration (`ORCH_ALLOW`) but has no documented "safe
    heredoc argument" story the way `tg`'s report body does — the carve-out's OWN consumer set is
    deliberately narrower (`tg` only, agent-tools#307 review round 2/9, Codex P2), so a
    heredoc fed to `git worktree list` must still hit the ordinary blanket block."""
    command = "git worktree list \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert ost.ORCH_ALLOW.search("git worktree list ") is not None  # sanctioned orchestration...
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # ...but not a consumer
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("head", ["tg", "tg ", "tg diff", "tg-ctl", "tg.sh", "tgfoo", "notg"])
def test_heredoc_safe_consumer_heads_are_a_subset_of_orch_allow(head):
    """Pins the relationship between the two hand-maintained allow-lists (agent-tools#307 review
    round 9, Fable): `_HEREDOC_SAFE_CONSUMER` and `ORCH_ALLOW` are NOT derived from each other, so
    nothing stops them silently diverging if one is edited without the other. The direction that
    would actually be unsafe is `_HEREDOC_SAFE_CONSUMER` accepting a head `ORCH_ALLOW` rejects (a
    heredoc argument carved out for a command that isn't even sanctioned orchestration at all) — so
    pin that every head the heredoc consumer regex matches is ALSO matched by `ORCH_ALLOW`, across
    both real `tg` shapes and near-miss impostors."""
    if ost._HEREDOC_SAFE_CONSUMER.search(head):
        assert ost.ORCH_ALLOW.search(head), head


@pytest.mark.parametrize("operator", ["&&", "||", ";", "|", "&", "\n"])
def test_segment_boundary_operator_set_agrees_with_split_chain(operator):
    """Pins the relationship Fable flagged (agent-tools#307 review round 11, finding #2):
    `_scan_segment_signals`'s docstring says it "mirrors `_split_chain`'s operator set" — a
    hand-maintained duplication, not a derived one. If the two ever silently diverge (a new
    operator taught to one but not the other), a heredoc span could be attributed to the WRONG
    segment's head — a laundering vector, not just cosmetic drift. Locks the operator set: for
    each real chain operator, `_scan_segment_signals`'s `seg_start` (where a NEW segment begins)
    must land at the SAME offset `_split_chain` uses to begin its own next segment."""
    command = f"echo a{operator}echo b"
    segs = ost._split_chain(command)
    assert len(segs) == 2, (operator, segs)
    expected_seg_start = len(command) - len(segs[1])
    masked, _depth, _in_comment, _in_single, _in_double = ost._mask_at_top_level(command, len(command))
    seg_start, _saw_redirect, _saw_poison = ost._scan_segment_signals(masked, len(command))
    assert seg_start == expected_seg_start, (operator, seg_start, expected_seg_start, segs)


def test_bare_command_substitution_placeholder_is_inert_on_its_own():
    """Pins the implicit contract the carve-out depends on (agent-tools#307 review round 9,
    Fable): collapsing a safe heredoc span to the neutral placeholder `$()` only works because NO
    current classifier treats a bare/empty command substitution as an implementation signal on its
    own. This guards that assumption independently of the heredoc path, so a future change that
    makes `$(...)` itself suspicious is forced to notice this test rather than silently breaking
    every heredoc carve-out call site."""
    assert ost._is_implementation_bash("tg $()") is False
    assert ost._is_implementation_bash('tg "$()"') is False


def test_heredoc_carveout_exception_fails_toward_the_blanket_block(monkeypatch, capsys):
    """Regression (agent-tools#307 review round 10, Fable): every other test in this file
    exercises `_strip_safe_heredoc_cat_substitutions` either directly (bypassing the guard in
    `_is_implementation_bash`) or via its normal, non-raising flow — none forced the defensive
    try/except itself to actually trip. Forces a real exception on that path and asserts BOTH that
    the command still hits the blanket block (fails toward BLOCK, the safe direction, matching
    `on_error: open`'s own bias) AND that the failure is OBSERVABLE via a `warn()` message, not a
    silently-swallowed `except: pass` — a systematic bug that raises on every input must be
    distinguishable from "the carve-out just never matches"."""
    def _boom(command):
        raise RuntimeError("boom")

    monkeypatch.setattr(ost, "_strip_safe_heredoc_cat_substitutions", _boom)
    command = "tg \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert ost._is_implementation_bash(command) is True
    captured = capsys.readouterr()
    assert "heredoc carve-out raised" in captured.err
    assert "boom" in captured.err


@pytest.mark.parametrize("command", [
    # Opus review round 2 Finding 1: an earlier, already-CLOSED "$(tg x)" substitution, or a
    # quoted `;tg;`, inside the same `eval` argument must not fool the head-check into reading
    # "tg" as the segment head when the real head is `eval`
    "eval \"x; tg\" \"$(cat <<'EOF'\nrm -rf ~\nEOF\n)\"",
])
def test_orch_allow_head_check_not_fooled_round_2(command, tmp_path, monkeypatch):
    """Second round of the same invariant Opus flagged: `_HEREDOC_SAFE_CONSUMER` (`^\\s*tg(?=\\s|$)`)
    is anchored, so `.search()` on the `command[seg_start:pos]` slice can only match at the slice's
    own start — `eval` is never mistaken for `tg` no matter what harmless or quoted-inert text
    precedes the heredoc (agent-tools#307 review round 2, Opus Finding 1)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    # Opus review #2: a command chained INSIDE the substitution, before the closing paren, must
    # not be swallowed by the "optional whitespace then `)`" immediacy check
    "tg \"$(cat <<'EOF'\nbody\nEOF\n; git push)\"",
    "tg \"$(cat <<'EOF'\nbody\nEOF\n && git push)\"",
])
def test_command_chained_inside_substitution_after_terminator_still_blocks(command, tmp_path, monkeypatch):
    """The substitution's closing `)` must IMMEDIATELY follow the terminator line (only
    whitespace/newlines between) — a real command squeezed in before it (`; git push`) runs INSIDE
    the `$(...)` in real bash and must never be swallowed by the carve-out (agent-tools#307, Opus
    review #2)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command  # untouched
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    # Opus review #3: `cat` with an EXTRA file argument also reads a real file, breaking the
    # "cat only ever echoes the heredoc" premise — must not collapse either way round
    "tg \"$(cat somefile <<'EOF'\nbody\nEOF\n)\"",
    "tg \"$(cat <<'EOF' somefile\nbody\nEOF\n)\"",
])
def test_cat_with_extra_file_argument_still_blocks(command, tmp_path, monkeypatch):
    """`cat somefile <<'EOF'...` or `cat <<'EOF' somefile...` also reads a REAL file, not just the
    heredoc — the safety argument ("cat only echoes stdin") only holds for a bare `cat` consuming
    nothing else, so this must never collapse (agent-tools#307, Opus review #3)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command  # untouched
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    # a body that IS entirely a flag-like string
    "tg \"$(cat <<'EOF'\n--help\nEOF\n)\"",
    # a body that opens with a flag-like line, even with more text after it
    "tg \"$(cat <<'EOF'\n--no-feature attach-denylist\nEOF\n)\"",
    # the flag-like line need not be the FIRST line of the body
    "tg \"$(cat <<'EOF'\nsome text\n--file /etc/passwd\nEOF\n)\"",
])
def test_flag_like_heredoc_body_still_blocks(command, tmp_path, monkeypatch):
    """Regression (agent-tools#307 review round 11, Codex P1): even after every OTHER gate in this
    file (exactly one argv element, never re-executed, never a redirect target), the resulting
    string is still handed to `tg` as literal message text — but `tg` extracts its OWN feature
    flags/options from argv BEFORE treating anything as message text. A heredoc BODY containing a
    dash-prefixed line (`--help`, `--no-feature ...`, `--file ...`) could change what `tg` DOES,
    not just what it prints, if the body's content isn't fully agent-authored (e.g. copied from
    upstream/external data). This is a narrow, cheap, safe-direction mitigation — it does not fully
    resolve `tg`'s own argv-parsing behavior (out of scope for this file; tracked in the
    agent-tools#307 follow-up ticket), but it closes the most obvious shape by refusing to
    collapse whenever any body line, once stripped, starts with `-`."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command  # untouched
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_bare_amp_split_after_safe_heredoc_is_pinned_at_the_split_layer():
    """Opus review #1 asked to CONFIRM a lone `&` mutation is actually caught, not laundered by
    the collapsed `tg`-headed segment. Pin it at the layer that actually does the work
    (`_split_chain`, independent of `_CMD_START`, which the reviewer correctly noted does NOT list
    a bare `&` — chain splitting and build-tool anchoring are two different mechanisms)."""
    stripped = 'tg "$()" & git push'
    segs = ost._split_chain(stripped)
    assert segs == ['tg "$()" ', ' git push']
    assert ost._seg_is_impl_signal(segs[1].strip()) is True


def test_orch_allowed_segment_head_helper_surface():
    """Pin `_heredoc_span_is_safe_orch_argument` directly — the gate that closes the Codex P1
    findings — independent of the full `_is_implementation_bash` pipeline."""
    safe = ost._heredoc_span_is_safe_orch_argument
    nested = 'tg "$('
    assert safe(nested, len(nested)) is False  # directly nested inside another still-open $(
    plain = 'tg "'
    assert safe(plain, len(plain)) is True  # plain tg argument, depth 0
    unsanctioned = 'eval "'
    assert safe(unsanctioned, len(unsanctioned)) is False  # segment head is not ORCH_ALLOW
    after_chain = 'review diff && tg "'
    assert safe(after_chain, len(after_chain)) is True  # segment head after && is tg
    quoted_ops = "tg 'a; b' \""
    assert safe(quoted_ops, len(quoted_ops)) is True  # `;` inside quotes never splits/opens a subst


@pytest.mark.parametrize("command", [
    # Opus review Finding 1: an EARLIER, already-CLOSED "$(tg x)" substitution inside the same
    # `eval` argument must not fool the head-check into reading "tg" as the segment head — the
    # segment head is `eval`, which is not ORCH_ALLOW, regardless of what harmless substitution
    # appears earlier on the same (double-quoted) line.
    "eval \"$(tg x) $(cat <<'EOF'\ngit commit -am wip\nEOF\n)\"",
    # ...same idea via a quoted `;tg;` that looks like a chain operator + orchestration head but is
    # inert literal text inside the double-quoted argument
    "eval \"a;tg;$(cat <<'EOF'\ngit commit\nEOF\n)\"",
])
def test_orch_allow_head_check_is_not_fooled_by_earlier_inert_tg_text(command, tmp_path, monkeypatch):
    """`ORCH_ALLOW` is anchored (`^\\s*(?:tg\\b|review\\b|...)`), so `.search()` on the
    `command[seg_start:pos]` slice can only match at the slice's own start — an `eval` (or any
    other unsanctioned) segment head is never mistaken for `tg`/`review` no matter what harmless
    substitution or literal-looking text precedes the heredoc within that same segment
    (agent-tools#307 review, Opus Finding 1 — verified NOT reproducible against the real
    ORCH_ALLOW definition, pinned here so the anchoring invariant can never silently regress)."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command, command  # untouched
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_paired_backticks_permanently_block_the_carveout_documented_bias(tmp_path, monkeypatch):
    """Opus review Finding 2: `_mask_at_top_level` treats a backtick as
    increment-ONLY (never decremented), so a properly PAIRED backtick substitution earlier in the
    same segment (e.g. a `` `date` `` in an earlier tg argument) permanently inflates depth and
    the carve-out never fires for a LATER, otherwise-safe heredoc in that command — a false BLOCK,
    not a security issue (the documented, safe-direction bias). Pinned so this is a known,
    intentional trade-off rather than a surprise regression report."""
    command = "tg \"built at `date`\" \"$(cat <<'EOF'\nreport\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # NOT collapsed
    assert ost._is_implementation_bash(command) is True  # false BLOCK, safe direction
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    # an escaped quote in an EARLIER argument (a legitimate title containing `\"`) desyncs
    # `in_double` in `_mask_at_top_level` the same way an attacker-controlled one would — the
    # carve-out cannot tell "legitimate escaped quote" from "attack" without real escape handling,
    # so it conservatively rejects both (Opus review round 5, Finding 1)
    'tg --title "the \\"big\\" report" "$(cat <<\'EOF\'\nbody\nEOF\n)"',
    # a mid-word `#` (bash does NOT treat this as a comment start — only a `#` preceded by
    # whitespace/start-of-word is a real comment) still trips `_mask_at_top_level`'s simpler
    # "any unquoted #" rule, over-blocking a harmless report body
    "tg report#final \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    # `$'...'` INSIDE double quotes is inert in real bash (no ANSI-C interpretation happens
    # there), but `_scan_segment_signals`'s `prev == \"$\"` check does not know that and still
    # treats it as a disqualifying ANSI-C opener
    "tg \"$'literal'\" \"$(cat <<'EOF'\nbody\nEOF\n)\"",
])
def test_documented_over_blocking_bias_is_safe_direction_not_a_bug(command, tmp_path, monkeypatch):
    """Opus review round 5: three cases where the carve-out's conservative checks (poison on ANY
    backslash, ANY unquoted `#`, or a `$'` sequence even inside double quotes) reject a
    LEGITIMATE, harmless `tg` report — never a security issue (it only ever falls back to the
    pre-existing blanket block, exactly as if the carve-out did not exist at all), but worth
    pinning explicitly so this is understood as an accepted, documented trade-off and never
    "fixed" later by loosening a check in the unsafe direction. Correctly distinguishing a
    legitimate escaped quote / mid-word `#` / double-quoted `$'...'` from an attack requires real
    shell escape/quote-context handling, out of scope for this discipline heuristic."""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # NOT collapsed
    assert ost._is_implementation_bash(command) is True  # false BLOCK, safe direction
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_poison_does_not_reset_across_a_real_chain_boundary_documented_bias(tmp_path, monkeypatch):
    """Review round 8 (Codex P2): `saw_poison` (backslash/`$'`/bare `(`) deliberately never
    resets at ANY segment boundary — including a perfectly ordinary, unambiguous `&&` between two
    genuinely separate commands — because round 4 proved the boundary detection itself can be
    the thing corrupted by exactly this signal (a fake `;` exposed by a `$'...'` misparse). The
    cost: a backslash in an EARLIER, wholly unrelated segment can over-block a LATER, otherwise
    perfectly safe `tg` heredoc report. Never a security issue (falls back to the pre-existing
    blanket block); pinned here as a known, accepted trade-off rather than a surprise regression
    report, since scoping poison correctly per-segment would require re-deriving segment
    boundaries in a way that is itself not corruption-proof (the same problem round 4 hit)."""
    command = "echo foo\\bar && tg \"$(cat <<'EOF'\nbody\nEOF\n)\""
    assert ost._strip_safe_heredoc_cat_substitutions(command) == command  # NOT collapsed
    assert ost._is_implementation_bash(command) is True  # false BLOCK, safe direction
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_stray_quote_in_earlier_heredoc_body_can_over_block_later_sibling_documented_bias(
    tmp_path, monkeypatch,
):
    """Review round 8 (Codex P2): `_mask_at_top_level` has no concept of "heredoc body" as an
    opaque zone — it is a generic quote/comment/depth tracker applied to the RAW command text, so
    a literal `"` inside an EARLIER heredoc's own body (fully inert to real bash, since the
    delimiter is single-quoted and the body is never parsed at all) still toggles that nesting
    level's `in_double` flag. This can blank a real newline that should have separated the body
    from its terminator line, which can in turn cause a LATER, otherwise perfectly legitimate
    sibling `tg` heredoc to be misjudged as still-nested and left uncollapsed. Never a security
    issue (over-blocks, does not launder anything — the contract that "body content is
    irrelevant" holds for a heredoc's OWN safety, just not always for a later sibling's
    classification); pinned as a known, accepted limitation. Making the tracker heredoc-body-
    aware (skipping `<<'DELIM'...DELIM` spans as fully opaque before doing quote/depth tracking
    at all) would close this properly but is a larger change deferred to a follow-up ticket."""
    command = "tg \"$(cat <<'A'\nsome \" quote\nA\n)\" && tg \"$(cat <<'B'\nbody\nB\n)\""
    stripped = ost._strip_safe_heredoc_cat_substitutions(command)
    assert "$()" in stripped  # the first heredoc still collapses on its own merits
    assert "<<'B'" in stripped  # the second, legitimate sibling is over-blocked (safe direction)
    assert ost._is_implementation_bash(command) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
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


# ── tg#7103: ALL `gh` is now DELEGATED — ship + CI/PR verification warn-then-block ─────────────

@pytest.mark.parametrize("command", [
    "gh ship 638",                                 # the sanctioned release — now a subagent's job
    "gh ship 638 && tg 'shipped'",                 # ship + report
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
    """ALL `gh` — ship, PR/CI verification, api (GET or mutation), every subcommand — is
    implementation the orchestrator must delegate to a subagent (Alex tg#7103, reverting the
    #159/#162 gh-ship carve-out). It is NOT in ORCH_ALLOW and IS a gh impl-signal, so a
    single unchained gh command warn-then-blocks exactly like `git commit`."""
    assert ost._seg_is_impl_signal(command.split("&&")[0].split("|")[0].strip()) is True, command
    assert ost.ORCH_ALLOW.search(command) is None, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_gh_is_not_inline_allowed():
    """`gh ship`/`gh pr` are no longer inline-allowed orchestration — they are implementation."""
    assert ost._is_all_inline_allowed("gh ship 5 && tg 'done'") is False
    assert ost._is_implementation_bash("gh ship 5") is True
    assert ost._is_implementation_bash("gh pr checks 5") is True
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
    """A path-qualified gh head (`/usr/bin/gh ship`) is normalized by basename and delegated too —
    the SAME normalization git gets (`/usr/bin/git commit`), closing the asymmetry a bare `^gh\\b`
    regex left open (Opus review). `gh` as a needle stays exempt; `gh-foo` is a different command."""
    assert ost._is_gh_command("/usr/bin/gh ship 605") is True
    assert ost._is_gh_command("/opt/homebrew/bin/gh pr checks 5") is True
    assert ost._is_implementation_bash("/usr/bin/gh ship 605 | tail -3") is True
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
    delegates end-to-end via the caller's strip."""
    assert ost._is_gh_command("GH_PAGER=cat gh ship 605") is True             # no pre-strip needed
    assert ost._is_gh_command("GH_PAGER=cat GH_TOKEN=x gh ship 605") is True  # multiple prefixes
    assert ost._is_gh_command(ost._strip_wrappers("GH_PAGER=cat gh ship 605")) is True
    assert ost._is_gh_command(ost._strip_wrappers("timeout 60 gh pr checks 5")) is True
    assert ost._is_gh_command("FOO=bar echo x") is False   # env-prefix on a non-gh head
    assert ost._is_implementation_bash("GH_PAGER=cat GH_TOKEN=x gh ship 605") is True
    # env-prefix AND an unbalanced quote together: _strip_wrappers bails on the bad quote, so the
    # env-prefix survives to the fallback regex, which skips leading VAR=val before the gh head.
    assert ost._is_implementation_bash("GH_PAGER=cat gh ship 605 'oops") is True
    assert ost._is_gh_command("GH_PAGER=cat GH_TOKEN=x gh ship 605 'oops") is True
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
    # `gh` is now delegated (tg#7103): any gh line is implementation, chained or not.
    assert ost._is_implementation_bash("gh ship 605 $(sed -i x)") is True
    assert ost._is_implementation_bash("gh ship 605 & git push") is True
    assert ost._is_implementation_bash("gh ship 605 --title 'a & b' | tail") is True  # gh head = impl
    assert ost._is_implementation_bash("gh ship 605 --note 'a; b' | tail") is True    # gh head = impl
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
    is a gh impl-signal. A gtimeout-wrapped ship is delegated too (the wrapper is stripped,
    exposing the `gh` head)."""
    for cmd in ("gh run list", "gh run view 9", "gh run watch 123"):
        assert ost.ORCH_ALLOW.search(cmd) is None, cmd
        assert ost._is_implementation_bash(cmd) is True, cmd
    assert ost._is_implementation_bash("gtimeout 60 gh ship 605 | tail") is True
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


# ── tg#7103: `gh ship` is now DELEGATED, not an orchestrator carve-out (reverts #159/#162) ────

@pytest.mark.parametrize("command", [
    "gh ship 605",                                             # bare ship — now a subagent's job
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
])
def test_gh_ship_now_delegated_warn_then_blocks(command, tmp_path, monkeypatch):
    """A `gh ship` line (any plumbing) is delegated to a subagent now (Alex tg#7103, reverting the
    #159/#162 orchestrator carve-out): shipping a gated PR is a subagent's job, so the orchestrator
    warn-then-blocks it exactly like `git commit`. The FIRST offense WARNs (advisory), a REPEAT in
    the window BLOCKs. (Read-only/`cd` companions do not rescue it — the `gh ship` segment head is
    itself the impl-signal.)"""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs (advisory)
    assert "message" in json.loads(out1), command           # ...and it DOES warn now
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


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
    """Under tg#7103 `gh ship` (and every gh) is DELEGATED — the reverse of the #159/#162 matrix.
    Asserted on `_is_implementation_bash` (True = blocked/delegated). ANY line whose gh segment
    head is `gh ship`/`gh pr`/… is implementation, regardless of read-only/`cd` plumbing, quoting,
    or substitutions — the gh head is itself the signal."""
    impl = ost._is_implementation_bash
    # every gh ship shape is now delegated (True), including the plumbing forms that used to pass
    assert impl("gh ship 605 | tail -3 | head -1") is True
    assert impl("gh ship 605 2>&1 | tail -3") is True
    assert impl("gh ship 605 --repo alex-mextner/agent-tools") is True
    assert impl("gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok") is True
    assert impl(
        "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3") is True
    assert impl("cd $(git rev-parse --show-toplevel) && gh ship 605") is True
    # quoted-metachar and single-quoted-substitution reasons block too — the gh head is the signal,
    # so quote handling no longer changes the verdict (it did under the #162 carve-out).
    assert impl("gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3") is True
    assert impl('gh ship 605 --title "a & b" | tail -3') is True
    assert impl("gh ship 605 --note 'ran $(build) earlier' | tail -3") is True
    assert impl('gh ship 605 --note "$(git push origin main)" | tail -3') is True
    assert impl("gh ship 605 --note '$(git push origin main)' | tail -3") is True
    # `gh ship` counts only at a segment HEAD (argv) — a needle in a read-only pipe is NOT a gh cmd
    assert impl("grep 'gh ship' log | head | wc -l") is False
    # companion mutations behind gh are moot now (gh already blocks), but the guards still hold
    assert impl("gh ship 605 && git push") is True
    assert impl("gh ship 605 & git push origin main") is True
    assert impl("tee >(wc -l) | gh ship 605") is True  # tee is BUILD_EDIT
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
