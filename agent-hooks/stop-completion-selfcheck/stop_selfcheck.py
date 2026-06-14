#!/usr/bin/env python3
"""agents-hooks/v1 stop hook — inject the completion self-check.

Fires when the agent is about to END its turn. It BLOCKS the stop exactly once,
returning the completion self-check prompt as the block message so the agent must run
the check before it can finish. A per-turn marker (keyed by the event's session id)
prevents an infinite stop→block→stop loop: the second time it sees the same session
it allows the stop.

This turns the `task-completion-selfcheck` ritual from advisory text into an enforced
prompt — the model is reliably nudged to (1) confirm everything was done and (2)
surface concrete follow-ups, instead of stopping at the first thing that worked.

Contract (agents-hooks/v1):
  stdin  : JSON event; a session/turn id in event.event_id (or args.session_id)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash must never trap the agent unable to stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "SELFCHECK_MARKER_DIR", "~/.cache/agent-tools/selfcheck")))
# After this many seconds a marker is considered stale (a new task in the same session).
MARKER_TTL_S = int(os.environ.get("SELFCHECK_TTL_S", "1800"))

PROMPT = (
    "Before finishing, run the completion self-check:\n"
    "1. Did I finish EVERYTHING in the request? Walk back through every clause — "
    "code, commits, push, deploy, docs, cleanup, artifacts. What did I miss?\n"
    "2. Concrete follow-ups: any bug I noticed, improvement worth a tracked issue, "
    "or dead code to remove? State each as 'do X because Y', then do it or record it.\n"
    "Back any 'done' claim with evidence you actually produced (test output, a "
    "screenshot you looked at, a command's exit code) — 'it should work' is not "
    "'it works'. If, after this, everything is genuinely done, you may finish."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"stop-selfcheck: {msg}\n")


def session_id(event: dict) -> str:
    args = event.get("args") or {}
    raw = (
        args.get("session_id")
        or event.get("event_id")
        or event.get("cwd")
        or "default"
    )
    return hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def marker_file(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.done"


def fresh(p: Path) -> bool:
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= MARKER_TTL_S
    except OSError:
        return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing stop (fail-open)")
        emit("allow")
        return 0

    sid = session_id(event)
    marker = marker_file(sid)

    if fresh(marker):
        # Already prompted this session/turn → let the agent stop.
        try:
            marker.unlink()  # consume it so a later genuine new task re-prompts
        except OSError:
            pass
        emit("allow")
        return 0

    # First stop for this session → record the marker and block once with the prompt.
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
    except OSError as exc:
        # Can't write a marker → don't risk a loop; allow the stop and just warn.
        warn(f"could not write marker {marker}: {exc} — allowing stop (fail-open)")
        emit("allow")
        return 0

    emit("block", PROMPT)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
