#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block pre-commit-gate bypasses.

Denies a shell command that would skip the pre-commit gate:
  - `git commit --no-verify` / `git commit -n`
  - `git push --no-verify`
  - setting HUSKY=0 / SKIP / LEFTHOOK=0 etc. inline to disable hooks
  - `git commit ... --no-verify` in any position

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (fall back to a few keys)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error for this hook is "closed": a parse failure or crash should DENY, because a
bypass that slipped through a broken gate is exactly what this hook exists to stop.
"""

from __future__ import annotations

import json
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# --no-verify or a bare -n flag on a git commit/push, in any argv position.
NO_VERIFY = re.compile(r"(?:^|\s)(?:--no-verify|-n)(?:\s|=|$)")
# Inline env-var tricks that disable common hook managers for one command.
HOOK_DISABLE_ENV = re.compile(
    r"(?:^|\s)(?:HUSKY=0|SKIP=\S+|LEFTHOOK=0|PRE_COMMIT_ALLOW_NO_CONFIG=\S+|"
    r"GIT_HOOKS_SKIP=\S+)\b"
)
GIT_COMMIT_OR_PUSH = re.compile(r"\bgit\b.*\b(commit|push)\b")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-no-verify: {msg}\n")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # on_error=closed → a parse failure denies. But emit a clear message so the
        # block is understandable, then exit 10.
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-no-verify: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = (
        args.get("command")
        or args.get("cmd")
        or event.get("command")
        or ""
    )
    if not isinstance(command, str):
        command = str(command)

    is_git = bool(GIT_COMMIT_OR_PUSH.search(command))

    if is_git and NO_VERIFY.search(command):
        emit(
            "block",
            "Refusing `git commit/push --no-verify` (-n): the pre-commit gate "
            "(lint/typecheck/tests) must not be bypassed. Fix the failing check "
            "instead of skipping the hook.",
        )
        return BLOCK_EXIT_CODE

    if HOOK_DISABLE_ENV.search(command):
        emit(
            "block",
            "Refusing to run with hooks disabled (HUSKY=0/SKIP/LEFTHOOK=0/...): the "
            "pre-commit gate must not be bypassed. Fix the failing check instead.",
        )
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
