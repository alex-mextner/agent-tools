#!/usr/bin/env python3
"""agents-hooks/v1 pre-agent hook — push non-trivial subagent dispatches to the background.

Fires when the MAIN thread is about to dispatch a subagent (the CC `Agent`/`Task` tool).
A non-trivial subagent run in the FOREGROUND blocks the orchestrator until it finishes —
which defeats the whole point of fanning work out. This gate blocks such a foreground
dispatch and tells the orchestrator to set ``run_in_background: true`` (or model the work
as a dynamic Workflow). It enforces the orchestration half of `delegate-work-to-subagents`.

Allowed (let through):
  - a dispatch already marked ``run_in_background: true`` (the desired shape)
  - a TRIVIAL one-liner dispatch (short, single-line prompt) — cheap enough to run inline
  - a dispatch made BY a subagent itself (subagent-exempt: ``agent_id`` present) — a subagent
    may fan out further, and this gate governs the orchestrator, not the workers

Escape hatch (controllable, not a hard wall — mirrors block-raw-pr-merge):
  - env  ALLOW_FOREGROUND_SUBAGENT=1            — disable the guard for this session
  - env  ALLOW_FOREGROUND_SUBAGENT_REASON=...   — REQUIRED with the override; logged
  A reasonless override still blocks. (No inline sentinel: the Agent tool carries no shell
  string to hide a `# ...` comment in.)

Contract (agents-hooks/v1):
  stdin  : JSON event; the dispatch payload is in args (run_in_background, prompt, description)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": this is an orchestration-discipline boundary, not a security gate — a
crash here must never wedge the ability to dispatch work.
"""

from __future__ import annotations

import json
import os
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A prompt is "trivial" (cheap to run inline) only if it is short AND single-line. Kept
# deliberately conservative: we block only a CLEARLY non-trivial foreground dispatch, so a
# false negative (a borderline one slipping through) is preferred to nagging on quick tasks.
TRIVIAL_MAX_CHARS = 200

REMINDER = (
    "Dispatch this subagent in the BACKGROUND. "
    "(1) The orchestrator must dispatch non-trivial subagents in the BACKGROUND. "
    "(2) Set `run_in_background: true` on the Agent/Task call, or model the work as a "
    "dynamic Workflow. "
    "(3) A foreground subagent blocks the main thread until it finishes — that defeats "
    "orchestration. "
    "Override only with a reason: ALLOW_FOREGROUND_SUBAGENT=1 + "
    "ALLOW_FOREGROUND_SUBAGENT_REASON='why'."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"background-subagent-gate: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _is_background(args: dict) -> bool:
    val = args.get("run_in_background")
    if isinstance(val, bool):
        return val
    return isinstance(val, str) and val.strip().lower() == "true"


def _is_trivial(args: dict) -> bool:
    """Trivial (cheap to run inline) only if BOTH prompt and description are short AND
    single-line. Judging on only the FIRST non-empty of the two let a `prompt:"x"` + a long
    `description` slip through as trivial and run in the foreground — defeating the gate. So we
    judge on the LONGEST of the two and require neither to carry a newline (#6)."""
    texts = []
    for key in ("prompt", "description"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            texts.append(val)
    if not texts:
        # No prompt/description to judge → don't claim triviality; let the other checks decide.
        return False
    if any("\n" in t.strip() for t in texts):
        return False  # a multi-line prompt OR description is non-trivial
    return max(len(t) for t in texts) < TRIVIAL_MAX_CHARS


def _override_reason() -> str | None:
    """The override reason if the env escape hatch is present WITH a reason, else None."""
    if os.environ.get("ALLOW_FOREGROUND_SUBAGENT") == "1":
        reason = (os.environ.get("ALLOW_FOREGROUND_SUBAGENT_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent may itself fan out — this gate governs the orchestrator, not the workers.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}

    if _is_background(args):
        emit("allow")  # already the desired shape
        return 0

    if _is_trivial(args):
        emit("allow")  # cheap one-liner — inline is fine
        return 0

    reason = _override_reason()
    if reason:
        warn(f"foreground subagent allowed via escape hatch ({reason})")
        emit("allow", f"foreground subagent allowed via escape hatch ({reason})")
        return 0

    emit("block", REMINDER)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
