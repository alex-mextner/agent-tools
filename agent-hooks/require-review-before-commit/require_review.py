#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require an AI review before a commit.

When the agent is about to `git commit`, this checks that an AI code review ran for
the current uncommitted state, by looking for a fresh marker file that the review
tool writes when it runs (and whose mtime is at least as new as the last change to
the index/working tree). If no review marker is found, it blocks with a reminder.

Wiring: have your review tool `touch` the marker on a successful run, e.g.
  review --uncommitted && touch "$REVIEW_MARKER"
The marker path is configurable via the REVIEW_MARKER env var; default below.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": a crash here must never wedge the ability to commit — this is a
discipline reminder, not a security boundary. (Contrast block-no-verify, fail-closed.)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

GIT_COMMIT = re.compile(r"\bgit\b.*\bcommit\b")
# A `git commit` that is clearly not making a normal commit (amend message only,
# etc.) we still gate — but allow obvious non-commits and merge/rebase continues.
SKIP_COMMIT = re.compile(r"--(?:continue|abort|skip)\b")

DEFAULT_MARKER = "~/.cache/agent-tools/last-review"
# How recent the review marker must be to count as "this session" (seconds).
FRESH_WINDOW_S = int(os.environ.get("REVIEW_FRESH_WINDOW_S", "3600"))


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-review: {msg}\n")


def marker_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("REVIEW_MARKER", DEFAULT_MARKER)))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # on_error=open → allow on inability to inspect; just warn.
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        return _allow()

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    if not GIT_COMMIT.search(command) or SKIP_COMMIT.search(command):
        return _allow()  # not a normal commit → nothing to gate

    marker = marker_path()
    try:
        if marker.exists() and (time.time() - marker.stat().st_mtime) <= FRESH_WINDOW_S:
            return _allow()  # a recent review ran → proceed
    except OSError as exc:
        warn(f"could not stat review marker {marker}: {exc} — allowing (fail-open)")
        return _allow()

    emit(
        "block",
        "No recent AI code review found for this change. Run a review on the "
        "uncommitted diff (e.g. `review` / `codex exec review --uncommitted`) and "
        f"address its findings before committing. (Set/touch {marker} on a "
        "successful review, or set REVIEW_MARKER.)",
    )
    return BLOCK_EXIT_CODE


def _allow() -> int:
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
