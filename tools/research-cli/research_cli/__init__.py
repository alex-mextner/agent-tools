"""research-cli — a multi-provider research/panel CLI on the agent-tools providers engine.

WHAT THIS IS
    A separate tool (NOT a review-cli mode) that runs a question past a *panel* of
    different models and synthesizes their answers into one cited research note. Code
    review and research are distinct surfaces; this tool keeps them separate while
    reusing the exact same multi-model plumbing review-cli uses.

HOW IT REACHES RUNTIME
    Entry point: ``bin/research`` -> :func:`research_cli.cli.main`. Commands are
    self-registering: drop a ``research_cli/commands/<name>.py`` exposing ``NAME``,
    ``SUMMARY`` and ``run(argv)`` and it becomes ``research <name>`` with zero edits to
    the dispatcher (self-registering-commands skill).

WHAT IT REUSES (verbatim) vs WHAT IT ADDS
    REUSES ``agenttools_providers`` (the merged shared CORE, agent-tools#49) for:
      - the capability-tagged model registry + role resolution (``resolve_role``),
      - the priority-ordered failover Board (``Board`` / ``board.split`` / ``pool``),
      - the key cascade (``KeyCascade``),
      - capability-tag filtering (``with_capability``).
    The providers CORE is deliberately NETWORK-FREE: it decides WHICH seat/key to use.
    This tool ADDS the transport half the CORE defers (see its README "Deferred"):
      - :mod:`research_cli.transport` — the pluggable runner that actually reaches a
        model (the availability predicate + the live call). Injectable, so the panel
        engine is testable with no network.
      - :mod:`research_cli.engine` — the single-round panel pass + synthesis (the MVP).

PHASING
    MVP (this scaffold): single-round, multi-provider fan-out + synthesis.
    Phased rest (tracked, see README "Roadmap"): multi-round follow-ups, adversarial
    cross-examination between seats, and source/citation verification.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
