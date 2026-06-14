#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — enforce a timeout on hangable commands.

Detects shell commands that can hang (build/test/install/network/browser/Electron)
and that are NOT wrapped in `timeout`/`gtimeout` (and don't carry an obvious
tool-level timeout flag). By default it WARNS via stderr and allows; in strict mode
(ENFORCE_TIMEOUT_STRICT=1) it BLOCKS so the agent re-issues the command with a bound.

Enforces the `shell-timeouts` skill. A hang leaves orphan processes and stalls the
session; an explicit bound turns a hang into a fast, actionable failure.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": never let this advisory check break the session.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

STRICT = os.environ.get("ENFORCE_TIMEOUT_STRICT") == "1"

# Commands that commonly hang and warrant a bound. Conservative: clear network /
# build / test / browser launchers, not every command.
HANGABLE = re.compile(
    r"\b("
    r"curl|wget|http|nc|ncat|"                       # network
    r"npm|pnpm|yarn|bun|pip|uv|cargo|go|mvn|gradle|"  # installs/builds
    r"jest|vitest|pytest|playwright|cypress|"         # test runners
    r"docker|docker-compose|kubectl|"                 # infra (can wait forever)
    r"electron|chromium|chrome|webkit"                # browser/Electron launches
    r")\b"
)
# Subcommands that are the hangable part (e.g. `npm test`, `go build`, `bun run`).
HANGABLE_SUB = re.compile(r"\b(install|ci|test|build|run|exec|fetch|pull|push|wait|launch)\b")

# Already bounded: wrapped in timeout/gtimeout, OR carries a timeout-ish flag.
HAS_TIMEOUT = re.compile(
    r"(?:^|\s|;|&&|\|\|)\s*(?:timeout|gtimeout)\s+\S+"   # timeout 60 ...
    r"|--timeout(?:[ =]\S+)?"                              # --timeout 60 / --timeout=60
    r"|--max-time\s+\S+"                                   # curl --max-time
    r"|-w\s+\S+\s+--timeout"                               # misc
)
# Read-only / instant commands that never need a bound, even if they match HANGABLE
# loosely (e.g. `npm version`, `go version`).
INSTANT = re.compile(r"\b(?:--version|-v|version|--help|-h|list|ls)\b")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"enforce-timeout: {msg}\n")


def needs_timeout(command: str) -> bool:
    if not HANGABLE.search(command):
        return False
    if HAS_TIMEOUT.search(command):
        return False
    if INSTANT.search(command) and not HANGABLE_SUB.search(command):
        return False
    # A network tool, or a build/test tool with a hangable subcommand.
    is_network = re.search(r"\b(curl|wget|http|nc|ncat)\b", command)
    return bool(is_network or HANGABLE_SUB.search(command))


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

    if needs_timeout(command):
        msg = (
            "This command can hang and has no timeout. Wrap it in `timeout N ...` "
            "(or pass an explicit tool-level timeout). A hang leaves orphan processes "
            "and stalls the session — bound it so a stall fails fast instead."
        )
        if STRICT:
            emit("block", msg)
            return BLOCK_EXIT_CODE
        warn(msg)
        emit("allow", msg)  # advisory: surface the message but proceed
        return 0

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
