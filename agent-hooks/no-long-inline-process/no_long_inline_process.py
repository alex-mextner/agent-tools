#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — keep long-running processes off the main thread.

A long-running process started inline by the ORCHESTRATOR blocks the main thread for
minutes — a multi-model `review`, a `--watch` loop, a full build/test suite, a long
`sleep`. This gate hard-BLOCKs such a command and tells the orchestrator to dispatch it
to a BACKGROUND subagent instead. It enforces the "orchestrator stays responsive" half of
`delegate-work-to-subagents` (the most clear-cut case → straight block, not warn-first).

Matched (conservative, anchored so a substring in a path/word never trips it):
  - the `review` CLI invoked as a command (start, or after && / ; / |) — minutes-long
  - any `--watch` flag (gh pr checks --watch, vitest --watch, tsc --watch, …)
  - a build/test SUITE: npm/pnpm/yarn/bun test|build, pytest/vitest/jest/cypress/playwright,
    cargo test|build, go test|build, make test|build|all
  - `sleep N[unit]` with a duration >= 10s (`sleep 5m` / `sleep 1h` count; `sleep 2` is fine)

Subagent-exempt: a dispatched subagent (``agent_id`` present) is EXPECTED to run these in
the background, so it is always allowed — this gate governs the orchestrator only.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_INLINE_PROCESS=1            — disable the guard for this session
  - env  ALLOW_INLINE_PROCESS_REASON=...   — REQUIRED with the override; logged
  - inline  `# inline-process-ok: <reason>`  — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A command starts at the line start or right after a &&/;/|/( separator. We anchor the
# long-process patterns on this so `review` inside a path (`/src/review/x`) or as a flag
# value never trips the guard — only an actual command invocation does.
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"

# The `review` CLI invoked as a command (multi-model, minutes-long).
REVIEW = re.compile(_CMD_START + r"review\b")
# Any --watch flag anywhere (gh pr checks --watch, vitest --watch, tsc --watch, …).
WATCH = re.compile(r"--watch\b")
# A build/test SUITE (each anchored at a command start to avoid substring false hits).
SUITE = re.compile(
    _CMD_START + r"(?:npm|pnpm|yarn|bun|deno)\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:pytest|vitest|jest|cypress|playwright)\b"
    r"|" + _CMD_START + r"cargo\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"go\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:make|rake|msbuild)\b[^&;|]*\b(?:test|build|all)\b"
    r"|" + _CMD_START + r"(?:mvn|gradle)\b[^&;|]*\b(?:test|build|verify|package)\b"
)
# `sleep N[unit]` invoked as a command (>= 10s is "long"). Anchored at a command start so
# `echo "sleep 100"` (the word inside a string) never trips it. The optional unit suffix
# (s/m/h/d) is parsed to seconds, so `sleep 5m` / `sleep 1h` are correctly seen as long —
# a bare `\d+` would read `5m` as 5 and wave a five-minute sleep through.
SLEEP = re.compile(_CMD_START + r"sleep\s+(\d+(?:\.\d+)?)([smhd]?)\b")
_SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
INLINE_SENTINEL = re.compile(r"#\s*inline-process-ok:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-long-inline-process: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_INLINE_PROCESS") == "1":
        reason = (os.environ.get("ALLOW_INLINE_PROCESS_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _matched_long_process(command: str) -> str | None:
    """Return a short label of the matched long-running process, or None."""
    if REVIEW.search(command):
        return "review (multi-model, minutes-long)"
    if WATCH.search(command):
        return "--watch (a watch loop never exits)"
    if SUITE.search(command):
        return "a build/test suite"
    m = SLEEP.search(command)
    if m:
        seconds = float(m.group(1)) * _SLEEP_UNIT_S[m.group(2)]
        if seconds >= 10:
            return f"sleep {m.group(1)}{m.group(2)} (long sleep)"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent is EXPECTED to run these in the background → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    matched = _matched_long_process(command)
    if matched is None:
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"inline long process allowed via escape hatch ({reason})")
        emit("allow", f"inline long process allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        f"Run this in a BACKGROUND subagent, not the orchestrator: `{matched}` is a "
        "long-running process (review / --watch / build-or-test suite / long sleep) that "
        "would block the main thread. Dispatch an Agent with run_in_background: true to run "
        "it. Override only with a reason: ALLOW_INLINE_PROCESS=1 + "
        "ALLOW_INLINE_PROCESS_REASON='why', or append `# inline-process-ok: why`.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
