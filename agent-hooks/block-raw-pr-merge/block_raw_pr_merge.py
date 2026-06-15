#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw `gh pr merge` that bypasses ship.

Denies a shell command that merges a PR directly with `gh pr merge` (including
`gh pr merge --admin`), because that skips the project's ship gate: `gh ship <PR>`
(the green-CI-gated merge + mandatory-screenshot command) is the only sanctioned path.
A raw merge lands code without the green-CI check and the required-screenshot check —
exactly the gates ship exists to enforce.

This is the enforcement counterpart of the doc-only "use `gh ship`, never a raw merge"
rule: advice in AGENTS.md cannot stop an autonomous (auto-mode) agent from running
`gh pr merge`, so the rule only becomes real as a mid-session guard.

Allowed (let through):
  - `gh ship <PR>` / a `gh alias` that runs ship
  - `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
  - any non-merge `gh pr` subcommand (view, list, checkout, create, comment, ...)

Escape hatch (controllable, not a hard wall — mirrors enforce-timeout / block-process-env):
  - env  ALLOW_RAW_PR_MERGE=1            — disable the guard for this session
  - env  ALLOW_RAW_PR_MERGE_REASON=...   — REQUIRED with the override; the reason is logged
  - inline sentinel  `# no-ship-guard: <reason>` anywhere in the command also overrides,
    so a one-off deliberate merge is self-documenting in the command itself.
  An override with no reason still blocks: a silent bypass of the bypass-guard is the very
  thing this hook exists to prevent.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a raw merge slipping through a
broken guard is exactly the failure this hook exists to stop.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# `gh pr merge` in any argv spacing (e.g. `gh   pr   merge`), --admin included implicitly.
GH_PR_MERGE = re.compile(r"\bgh\b\s+pr\s+merge\b")
# The sanctioned ship paths — never block these even though they ultimately merge.
SHIP_ALLOW = re.compile(r"\bgh\b\s+ship\b|\bpr-ship\.sh\b|\bship\.sh\b")
# Inline, self-documenting per-command override.
INLINE_SENTINEL = re.compile(r"#\s*no-ship-guard:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


def _override_reason(command: str) -> str | None:
    """Return the override reason if a valid escape hatch is present, else None.

    An override is honored ONLY with a reason: env ALLOW_RAW_PR_MERGE=1 plus
    ALLOW_RAW_PR_MERGE_REASON, OR an inline ``# no-ship-guard: <reason>`` sentinel.
    A reasonless override is ignored (the merge stays blocked).
    """
    if os.environ.get("ALLOW_RAW_PR_MERGE") == "1":
        reason = (os.environ.get("ALLOW_RAW_PR_MERGE_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-raw-pr-merge: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    # not a raw merge → nothing to do (covers gh ship, gh pr view/list/checkout, etc.)
    if not GH_PR_MERGE.search(command) or SHIP_ALLOW.search(command):
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"raw `gh pr merge` allowed via escape hatch ({reason})")
        emit("allow", f"raw `gh pr merge` allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        "Refusing a raw `gh pr merge` (incl. --admin): it bypasses the ship gates "
        "(green CI + required screenshots). Use `gh ship <PR>` instead — it merges only "
        "once CI is green and the mandatory screenshots are present. Override only with an "
        "explicit reason: set ALLOW_RAW_PR_MERGE=1 and ALLOW_RAW_PR_MERGE_REASON='why', or "
        "append `# no-ship-guard: why` to the command.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
