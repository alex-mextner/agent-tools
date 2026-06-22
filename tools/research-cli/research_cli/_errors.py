"""_errors — research-cli's single import point for the shared ``agenttools_errors`` layer.

WHAT THIS FILE IS
    A thin shim that re-exports the ecosystem's structured-error API (error-system v2:
    WHAT / WHY / HOW-to-fix + stable per-class exit codes) so every research-cli module
    imports it from ONE place (``from ._errors import UsageError, guard, …``) instead of each
    re-doing the ``lib/`` ``sys.path`` dance.

HOW IT'S REACHED AT RUNTIME
    The dispatcher and each command import the names they raise/handle from here. The shared
    package lives at ``lib/agenttools_errors`` in the agent-tools umbrella; like ``providers.py``
    does for ``agenttools_providers``, this adds ``lib/`` to ``sys.path`` so the import resolves
    from a SOURCE checkout with no install step. When research-cli is spun out to its own repo it
    depends on the published ``agenttools-errors`` distribution and this shim drops the path hack.

INVARIANTS
    - **Stdlib-only at import** (lazy-heavy-imports skill): ``agenttools_errors`` is itself
      stdlib-only, so importing this shim keeps ``research --help`` / ``--version`` fast and
      offline — the whole reason the dispatcher can import it at module top.
    - Re-export, don't re-implement: the error classes/builders are the SHARED ones, so a fix in
      the lib lands here for free (shared-util-single-source skill).
"""

from __future__ import annotations

import sys
from pathlib import Path

# research-cli is scaffolded next to lib/ in the umbrella; add lib/ so agenttools_errors
# resolves from a source checkout (the same pattern providers.py uses for the providers CORE:
# this file is tools/research-cli/research_cli/_errors.py, so parents[3] is the repo root).
_LIB = Path(__file__).resolve().parents[3] / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_errors import (  # noqa: E402  (after sys.path injection)
    EXIT_INTERNAL,
    EXIT_NETWORK,
    EXIT_UNKNOWN_ITEM,
    EXIT_USAGE,
    AgentToolError,
    NetworkError,
    UsageError,
    guard,
    unknown_item_error,
)

# The full set of exit-code constants research-cli can return, re-exported so this shim is the
# ONE import point (the unknown-command path raises an UnknownItemError -> EXIT_UNKNOWN_ITEM, so
# that code is part of research-cli's public contract and belongs here too). EXIT_NETWORK is
# carried by NetworkError; it's re-exported for callers/tests that branch on the code directly.
__all__ = [
    "EXIT_INTERNAL",
    "EXIT_NETWORK",
    "EXIT_UNKNOWN_ITEM",
    "EXIT_USAGE",
    "AgentToolError",
    "NetworkError",
    "UsageError",
    "guard",
    "unknown_item_error",
]
