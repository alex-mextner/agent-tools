"""model-freshness checker — keeps lib/contracts/models.yaml current.

Accessed via: `python3 lib/checker/model_freshness.py` (the daily cron rig provisions),
or imported as `from checker.model_freshness import run` in tests.

The public surface is intentionally small — `model_freshness` owns the logic; this package
init only re-exports it so `import checker` works for ad-hoc use.
"""

from __future__ import annotations

__all__ = ["model_freshness"]
