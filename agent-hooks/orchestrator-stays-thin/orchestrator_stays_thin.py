#!/usr/bin/env python3
"""agents-hooks/v1 pre-write + pre-bash hook — keep the orchestrator thin.

The orchestrator plans, dispatches, and verifies — it does NOT implement inline. When the
MAIN thread is about to do implementation-shaped work itself (a non-docs code Edit/Write, or
a multi-step implementation Bash) this gate nudges it to delegate to a subagent or a
Workflow. It enforces `delegate-work-to-subagents`.

ONE script binds TWO points via two descriptors; it branches on ``event["point"]``:
  - pre-write : a CODE Edit/Write (non-docs) by the main thread → warn-then-block
  - pre-bash  : a clearly multi-step / implementation-shaped Bash by the main thread →
                warn-then-block. Read-only inspection is NEVER blocked — a single one-liner
                (git status, ls, cat, grep, find) OR a fully read-only chain of any length
                across |/&&/;/||/newline (find ... | grep ... | head, git status && ls).

TIERED (warn → block): the FIRST offense in the TTL window WARNs (allow + message); a REPEAT
in the window BLOCKs. The tier is tracked by a marker file keyed by a hash of cwd. This gives
the doctrine's "WARN then BLOCK" instead of a hard wall on the first inline edit.

Subagent-exempt: a dispatched subagent (``agent_id`` present) does the actual work, so it is
always allowed — this gate governs the orchestrator only.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_ORCHESTRATOR_WORK=1            — disable the guard for this session (both points)
  - env  ALLOW_ORCHESTRATOR_WORK_REASON=...   — REQUIRED with the override; logged
  - inline (PRE-BASH ONLY)  `# orchestrator-ok: <reason>`  — self-documenting per-command.
    A pre-write carries no shell string, so the inline sentinel can only fire for a bash
    command; for a write use the ENV hatch.
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command (bash) or args.file_path/path (write); event.point
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orchestrator-thin")))
# How long a first-offense WARN suppresses the next WARN before a REPEAT becomes a BLOCK.
TTL_S = int(os.environ.get("ORCH_THIN_TTL_S", "900"))

# A write to one of these is documentation, never implementation → always allow.
DOCS_PATH = re.compile(r"\.(?:md|mdx|txt|rst)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)

# Inspection / read-only one-liners that the orchestrator legitimately runs itself.
READ_ONLY_BASH = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|less\b|head\b|tail\b|"
    r"grep\b|rg\b|find\b|pwd\b|echo\b|which\b|env\b|wc\b|stat\b|tree\b|file\b)"
)
# Any chain operator (&&, ||, ;, |, newline). A command that chains AT ALL is not a single
# read-only invocation, so the read-only carve-out below must NOT short-circuit it (B1).
CHAIN = re.compile(r"&&|\|\||;|\||\n")
# A command starts at the line start or right after a &&/;/|/( separator. Anchoring the
# build-tool tokens here means the runner must be the COMMAND, not an argument/needle —
# the same anchoring the no-long-inline-process sibling uses (#5).
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"
# A heredoc, or an obvious build/edit invocation, marks implementation-shaped shell.
HEREDOC = re.compile(r"<<-?\s*['\"]?\w+")
# In-place edit / build invocations. A bare `>`/`>>` redirect is NOT here: a redirect alone
# ("python foo.py > out.log") is not implementation — the in-place editors (sed -i, tee) and
# the build tools are the real signals (B7). The build-tool RUNNERS are anchored at a command
# head so a substring needle in an inspection pipe (`cat notes.md | grep npm`, `git log | rg
# yarn`, `find . -name cargo.toml | wc -l`) is NOT mis-read as implementation (#5). The
# in-place editors (sed -i, tee) keep a bare `\b` anchor: they are a content signal wherever
# they appear (e.g. `git status && sed -i ...`).
BUILD_EDIT = re.compile(
    r"\b(?:sed\s+-i|tee)\b"
    r"|" + _CMD_START + r"(?:npm|pnpm|yarn|bun|cargo|go\s+build|make|"
    r"python\s+setup|pip\s+install)\b"
)
INLINE_SENTINEL = re.compile(r"#\s*orchestrator-ok:\s*(\S.*)")

MESSAGE = (
    "Delegate to a subagent: the orchestrator plans, dispatches, and verifies — it does not "
    "implement inline. Launch an Agent (run_in_background: true) or a Workflow to do this "
    "Edit/Write/Bash. (delegate-work-to-subagents, enforced.)"
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"orchestrator-stays-thin: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str, point: str) -> str | None:
    """The override reason if a valid escape hatch is present, else None.

    Honored ONLY with a reason:
      - env ALLOW_ORCHESTRATOR_WORK=1 + ALLOW_ORCHESTRATOR_WORK_REASON (both points), OR
      - an inline ``# orchestrator-ok: <reason>`` — PRE-BASH ONLY. A pre-write carries no shell
        string, so the inline sentinel genuinely cannot fire for a write; only the ENV hatch
        applies there (B4). A reasonless override is ignored.
    """
    if os.environ.get("ALLOW_ORCHESTRATOR_WORK") == "1":
        reason = (os.environ.get("ALLOW_ORCHESTRATOR_WORK_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    if point == "pre-bash":
        m = INLINE_SENTINEL.search(command)
        if m:
            return f"inline override: {m.group(1).strip()}"
    return None


def _marker(event: dict) -> Path:
    # Key by (cwd, point) so a pre-write WARN does not prime a pre-bash BLOCK (or vice versa):
    # each point tiers independently (B5).
    cwd = str(event.get("cwd") or "default")
    point = str(event.get("point") or "")
    sid = hashlib.sha256(f"{cwd}\0{point}".encode()).hexdigest()[:16]
    return MARKER_DIR / f"{sid}.warned"


def _is_repeat(event: dict) -> bool:
    """True if a WARN already fired in this cwd within the TTL window (→ now BLOCK)."""
    m = _marker(event)
    try:
        if m.exists() and (time.time() - m.stat().st_mtime) <= TTL_S:
            return True
    except OSError:
        return False
    # First offense → record the warn marker so the next one in the window blocks.
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        m.write_text(str(time.time()))
    except OSError as exc:
        warn(f"could not write warn marker {m}: {exc} — staying in WARN tier")
    return False


def _is_code_write(args: dict) -> bool:
    # NotebookEdit carries `notebook_path`; the bridge also aliases it onto file_path/path, but
    # read it directly too so a `.ipynb` edit is judged even if only notebook_path is set (B3).
    path = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
    if not isinstance(path, str) or not path.strip():
        return False  # no path to judge → don't claim it's a code write
    if DOCS_PATH.search(path) or DOCS_DIR.search(path):
        return False  # docs are not implementation
    return True


def _is_all_read_only(command: str) -> bool:
    """True when EVERY chain segment's HEAD is a read-only inspection command.

    A fully read-only pipe (`find ... | grep ... | head`, `tail X | grep Y | wc -l`) is the
    orchestrator's bread-and-butter inspection and must never be blocked, no matter how many
    segments it has (#80).

    The judgement is per-segment-HEAD, NOT a whole-string scan: a build/edit token that appears
    only as an ARGUMENT/needle of a read-only command (`cat tee.log`, `grep cargo notes.txt`)
    must stay allowed — exactly the single-command carve-out this replaced (`tee`/`sed -i` are
    unanchored in BUILD_EDIT, so a whole-string scan would mis-flag them). A real build/edit or
    heredoc segment has a NON-read-only head (`sed ...`, `tee ...`, `npm ...`, the heredoc body),
    so it breaks `all(...)` and the caller then judges the command as implementation. HEREDOC is
    additionally vetoed up front — no inspection one-liner contains a `<<WORD` redirect.
    """
    if HEREDOC.search(command):
        return False
    # NOTE: judged on the segment HEAD only — READ_ONLY_BASH is head-anchored (`^\s*…`), so a
    # build/edit head with a read-only word in its args (`sed -i … grep.py`) is NOT waved through
    # (regression-guarded by test_build_edit_head_with_read_only_needle_blocks). Inherited gap
    # (pre-#80, also true of the old single-command carve-out): a read-only HEAD with a mutating
    # flag — `find . -delete`, `find . -exec sed -i …` — reads as inspection. Out of scope here.
    segments = [s for s in CHAIN.split(command) if s.strip()]
    return bool(segments) and all(READ_ONLY_BASH.search(s) for s in segments)


def _is_implementation_bash(command: str) -> bool:
    if not command.strip():
        return False
    # A fully read-only pipe of ANY length is inspection, never implementation (#80). The older
    # single-command carve-out was a subset of this; an all-read-only chain now passes too.
    if _is_all_read_only(command):
        return False
    # A chain that merely STARTS with a read-only command (`git status && sed -i ...`,
    # `ls; npm run build`) is judged on its full content, not waved through on its prefix (B1).
    if HEREDOC.search(command) or BUILD_EDIT.search(command):
        return True
    # Multiple chained steps (>= 2 operators, i.e. > 2 commands joined) is implementation-shaped.
    return len(CHAIN.findall(command)) >= 2


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # Subagents do the actual work → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    point = event.get("point") or ""
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    if point == "pre-write":
        offending = _is_code_write(args)
    elif point == "pre-bash":
        offending = _is_implementation_bash(command)
    else:
        offending = False

    if not offending:
        emit("allow")
        return 0

    reason = _override_reason(command, point)
    if reason:
        warn(f"orchestrator work allowed via escape hatch ({reason})")
        emit("allow", f"orchestrator work allowed via escape hatch ({reason})")
        return 0

    # WARN first, BLOCK on repeat within the window.
    if _is_repeat(event):
        emit("block", MESSAGE)
        return BLOCK_EXIT_CODE
    warn(MESSAGE)
    emit("allow", MESSAGE)  # advisory first offense
    return 0


if __name__ == "__main__":
    sys.exit(main())
