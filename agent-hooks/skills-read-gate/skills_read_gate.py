#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require mandatory skills before doing WORK.

Before a work-shaped action (a `git commit`, or a build/test command), this checks that the
mandatory/relevant skills were INVOKED this session. A skill encodes the rules the action
must follow (e.g. `delegate-work-to-subagents` before dispatching, `visual-proof-cycle`
before a UI "done"); doing the work without first reading the skill skips those rules.

How it knows a skill was invoked — the MARKER CONTRACT:
  A skill-invocation wrapper touches one file per invoked skill in a marker dir:
      ~/.cache/agent-tools/skills-invoked/<skill-name>      (mtime = invocation time)
  A skill counts as invoked if its marker is FRESH (within SKILLS_FRESH_WINDOW_S, default
  7200s). The wrapper that touches it is the honest, satisfiable action — see the README.
  Configure the dir with SKILLS_INVOKED_DIR; the mandatory set with MANDATORY_SKILLS
  (comma-separated, default `delegate-work-to-subagents,visual-proof-cycle`).

TIERED (warn → block): if a mandatory skill has no fresh marker, the FIRST work action in
the window WARNs (allow + message); a REPEAT BLOCKs (tracked by a marker keyed by cwd, like
orchestrator-stays-thin). Default tier is WARN-FIRST so it never wedges while the
marker-writer is still being wired everywhere.

NOTE: NOT subagent-exempt — a subagent doing work should also have read its skills.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_SKIP_SKILLS=1            — disable the guard for this session
  - env  ALLOW_SKIP_SKILLS_REASON=...   — REQUIRED with the override; logged
  - inline  `# skills-ok: <reason>`     — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": process discipline, not a security boundary — a crash must never wedge
the ability to commit/build.
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

INVOKED_DIR = Path(os.path.expanduser(os.environ.get(
    "SKILLS_INVOKED_DIR", "~/.cache/agent-tools/skills-invoked")))
TIER_DIR = Path(os.path.expanduser(os.environ.get(
    "SKILLS_TIER_DIR", "~/.cache/agent-tools/skills-read-tier")))
FRESH_WINDOW_S = int(os.environ.get("SKILLS_FRESH_WINDOW_S", "7200"))

DEFAULT_MANDATORY = "delegate-work-to-subagents,visual-proof-cycle"

# Work-shaped actions this gate fires on: a commit, or a build/test command.
# Anchored to a COMMAND invocation (line start, or after a |/&/; separator) with `commit` as
# git's subcommand. Global flags AND their values (`git -C /repo commit`, `git -c k=v commit`)
# are allowed between `git` and `commit`, but the run may NOT cross a command separator — so
# plain text such as `echo "remember to git, then commit"` does NOT trip it (B2).
GIT_COMMIT = re.compile(r"(?:^|[|&;]\s*)git(?:[ \t]+[^\s;&|]+)*?[ \t]+commit\b")
SKIP_COMMIT = re.compile(r"--(?:continue|abort|skip)\b")
# A command starts at the line start or right after a &&/;/|/( separator. The build/test
# runner must be at this command HEAD — not buried inside a string argument — so that
# `git commit -m "fix: npm test was flaky"` and `echo "see npm test output"` are NOT
# mis-classified as build/test work. Same anchoring the no-long-inline-process sibling uses (#4).
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"
BUILD_OR_TEST = re.compile(
    _CMD_START + r"(?:npm|pnpm|yarn|bun|deno)\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:pytest|vitest|jest)\b"
    r"|" + _CMD_START + r"cargo\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"go\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:make|rake|msbuild)\b[^&;|]*\b(?:test|build|all)\b"
    r"|" + _CMD_START + r"(?:mvn|gradle)\b[^&;|]*\b(?:test|build|verify|package)\b"
)
INLINE_SENTINEL = re.compile(r"#\s*skills-ok:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"skills-read-gate: {msg}\n")


def _mandatory_skills() -> list[str]:
    raw = os.environ.get("MANDATORY_SKILLS", DEFAULT_MANDATORY)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _is_work_action(command: str) -> bool:
    if GIT_COMMIT.search(command) and not SKIP_COMMIT.search(command):
        return True
    return bool(BUILD_OR_TEST.search(command))


def _fresh(p: Path) -> bool:
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= FRESH_WINDOW_S
    except OSError:
        return False


def _missing_skills() -> list[str]:
    return [s for s in _mandatory_skills() if not _fresh(INVOKED_DIR / s)]


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_SKIP_SKILLS") == "1":
        reason = (os.environ.get("ALLOW_SKIP_SKILLS_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _tier_marker(event: dict) -> Path:
    cwd = str(event.get("cwd") or "default")
    sid = hashlib.sha256(cwd.encode()).hexdigest()[:16]
    return TIER_DIR / f"{sid}.warned"


def _is_repeat(event: dict) -> bool:
    """True if a WARN already fired in this cwd within the window (→ now BLOCK)."""
    m = _tier_marker(event)
    try:
        if m.exists() and (time.time() - m.stat().st_mtime) <= FRESH_WINDOW_S:
            return True
    except OSError:
        return False
    try:
        TIER_DIR.mkdir(parents=True, exist_ok=True)
        m.write_text(str(time.time()))
    except OSError as exc:
        warn(f"could not write tier marker {m}: {exc} — staying in WARN tier")
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    if not _is_work_action(command):
        emit("allow")  # not a work-shaped action → nothing to gate
        return 0

    missing = _missing_skills()
    if not missing:
        emit("allow")  # every mandatory skill has a fresh marker → satisfied
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"skills gate skipped via escape hatch ({reason})")
        emit("allow", f"skills gate skipped via escape hatch ({reason})")
        return 0

    message = (
        f"Mandatory/relevant skills not invoked before this work: {', '.join(missing)}. "
        "Invoke them (Skill tool) first — they encode the rules this action must follow. "
        "(e.g. delegate-work-to-subagents before dispatching, visual-proof-cycle before a "
        "UI 'done'.) Override only with a reason: ALLOW_SKIP_SKILLS=1 + "
        "ALLOW_SKIP_SKILLS_REASON='why', or append `# skills-ok: why`."
    )

    # WARN first, BLOCK on repeat within the window.
    if _is_repeat(event):
        emit("block", message)
        return BLOCK_EXIT_CODE
    warn(message)
    emit("allow", message)  # advisory first offense (marker-writer may not be wired yet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
