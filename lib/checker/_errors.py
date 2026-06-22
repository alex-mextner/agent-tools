"""_errors — the model-freshness checker's single import point for ``agenttools_errors``.

WHAT THIS FILE IS
    A thin shim that re-exports the ecosystem's structured-error API (error-system v2:
    WHAT / WHY / HOW-to-fix + stable per-class exit codes) so ``model_freshness`` imports it
    from ONE place (``from ._errors import UsageError, guard, …``) instead of re-doing the
    ``lib/`` ``sys.path`` dance. Mirrors ``tools/research-cli/research_cli/_errors.py`` (the
    research-cli adoption, PR #85), kept identical in spirit so a fix in the shared lib lands
    everywhere for free.

HOW IT'S REACHED AT RUNTIME
    ``model_freshness.main`` imports the names it raises/handles from here and wraps its body in
    :func:`guard`. The shared package lives at ``lib/agenttools_errors`` in the same umbrella —
    this file is ``lib/checker/_errors.py``, so ``parents[1]`` is ``lib/`` — added to
    ``sys.path`` so the import resolves from a SOURCE checkout with no install step (the cron
    runs ``python3 lib/checker/model_freshness.py`` directly).

INVARIANTS
    - **Stdlib-only at import** (lazy-heavy-imports skill): ``agenttools_errors`` is itself
      stdlib-only, so importing this shim keeps ``--validate`` / ``--help`` fast and offline —
      the checker's one real dependency (PyYAML) is still lazy-imported only when the manifest
      is actually read.
    - Re-export, don't re-implement: the error classes/builders are the SHARED ones, so a fix in
      the lib lands here for free (shared-util-single-source skill).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The checker is at lib/checker/; add lib/ so agenttools_errors resolves from a source checkout
# (this file is lib/checker/_errors.py, so parents[1] is the lib/ dir the shared package lives in).
_LIB = Path(__file__).resolve().parents[1]
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_errors import (  # noqa: E402  (after sys.path injection)
    EXIT_USAGE,
    UsageError,
    guard,
)

# The exact set the checker uses: it raises ``UsageError`` and renders via ``guard``; ``EXIT_USAGE``
# is the only code it returns on a diagnosed failure (re-exported so this shim is the ONE import
# point, and so tests can branch on the code). A malformed/unreadable manifest and a failing
# --validate are both the usage class (EXIT_USAGE=2) per the structured-exit-codes skill.
__all__ = [
    "EXIT_USAGE",
    "UsageError",
    "guard",
]
