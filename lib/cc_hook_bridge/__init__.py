"""cc-hook-bridge — make `agents-hooks/v1` descriptors actually FIRE in Claude Code.

WHAT THIS IS
    A single dispatcher that Claude Code's ``settings.json`` PreToolUse and Stop hooks
    call. Claude Code only runs hooks declared in ``settings.json``; it never reads the
    ``~/.claude/hooks/*.json`` ``agents-hooks/v1`` descriptors rig installs. So without
    this bridge EVERY agent-hook (block-raw-pr-merge, block-secrets-write, …) is inert in
    CC (see agent-tools#18). This dispatcher closes that gap. (PostToolUse is intentionally
    NOT wired — the tool has already run by then, so it cannot block; see the README.)

HOW IT'S REACHED AT RUNTIME
    rig wires one entry per event into the harness ``settings.json``:
        PreToolUse  (matcher Bash)                 → python3 -m cc_hook_bridge PreToolUse
        PreToolUse  (matcher Edit|Write|MultiEdit…) → python3 -m cc_hook_bridge PreToolUse
        Stop                                       → python3 -m cc_hook_bridge Stop
    CC feeds the tool-call JSON on stdin. The dispatcher selects + runs the installed
    descriptors and translates the v1 exit-10 BLOCK into CC's own block signal.

INVARIANTS
    - stdlib only at import time (the repo's lazy-import rule); no third-party deps.
    - Fail-OPEN on a dispatcher error (a broken bridge must never wedge every tool call).
    - Fail-CLOSED when a hook explicitly blocks, or when a hook errors and its descriptor
      says ``on_error: "closed"`` — that's exactly the security-gate semantics of v1.
    - The CC block signal is event-specific (confirmed against the live docs, CC 2.1.177):
      PreToolUse → exit 0 + ``hookSpecificOutput.permissionDecision: "deny"``;
      Stop       → exit 0 + top-level ``decision: "block"``.
"""

from __future__ import annotations

from . import dispatch

__all__ = ["dispatch"]
