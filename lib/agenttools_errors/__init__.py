"""agenttools_errors — one shared error/exit-code layer for every agent-tools CLI.

Every ecosystem CLI (review / rig / tg / draw / 3d / task) wants the SAME shape for a
failure: a three-part message — WHAT went wrong, WHY (root cause + offending file/context),
HOW to fix it (a concrete command) — plus a stable, per-class EXIT CODE so a calling script
can branch on the failure class. Instead of each tool hand-rolling that, this is the single
shared copy the roadmap's error-system-v2 item calls for.

Quick start
-----------
    from agenttools_errors import (
        AgentToolError, UsageError, MissingDepError,
        guard, render, did_you_mean, require_tool,
        EXIT_USAGE, EXIT_MISSING_DEP,
    )

    def run() -> int:
        if not os.path.exists(cfg):
            raise UsageError(
                what=f"config not found: {cfg}",
                why="the --config path doesn't exist",
                fix=f"create {cfg} or pass --config <path>",
            )
        openscad = require_tool(
            "openscad",
            needed_for="to produce the mesh",
            install="brew install openscad",
            rerun="3d render model.scad",
        )
        ...
        return 0

    raise SystemExit(guard(run))   # renders the 3-part block + returns the stable exit code

The exit-code constants (``EXIT_OK`` … ``EXIT_MISSING_DEP``) are a PUBLIC CONTRACT — scripts
and CI branch on them; ``EXIT_CODES`` is the table for ``--help`` / docs. The full reference
lives in ``lib/agenttools_errors/README.md``.
"""

from __future__ import annotations

from .core import (
    EXIT_CODES,
    EXIT_CONFIG,
    EXIT_DRIFT,
    EXIT_INTERNAL,
    EXIT_MISSING_DEP,
    EXIT_MISSING_TARGET,
    EXIT_NETWORK,
    EXIT_NOT_A_REPO,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_UNKNOWN_ITEM,
    EXIT_USAGE,
    AgentToolError,
    ConfigError,
    DriftError,
    MissingDepError,
    MissingTargetError,
    NetworkError,
    NotARepoError,
    PermissionDeniedError,
    RemovedSlot,
    RemovedSlotRegistry,
    UnknownItemError,
    UsageError,
    did_you_mean,
    guard,
    missing_dep_error,
    missing_target_error,
    not_a_repo_error,
    render,
    require_tool,
    should_color,
    unknown_item_error,
)

__all__ = [
    # exit codes
    "EXIT_OK",
    "EXIT_INTERNAL",
    "EXIT_USAGE",
    "EXIT_CONFIG",
    "EXIT_DRIFT",
    "EXIT_UNKNOWN_ITEM",
    "EXIT_MISSING_TARGET",
    "EXIT_NOT_A_REPO",
    "EXIT_NETWORK",
    "EXIT_PERMISSION",
    "EXIT_MISSING_DEP",
    "EXIT_CODES",
    # error types
    "AgentToolError",
    "UsageError",
    "ConfigError",
    "DriftError",
    "UnknownItemError",
    "MissingTargetError",
    "NotARepoError",
    "NetworkError",
    "PermissionDeniedError",
    "MissingDepError",
    # rendering + handler
    "render",
    "guard",
    "should_color",
    # heuristics
    "did_you_mean",
    "RemovedSlot",
    "RemovedSlotRegistry",
    "unknown_item_error",
    "missing_dep_error",
    "require_tool",
    "missing_target_error",
    "not_a_repo_error",
]

__version__ = "0.1.0"
