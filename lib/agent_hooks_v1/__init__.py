"""Shared agents-hooks/v1 descriptor runner.

Accessed via: harness bridges such as ``cc_hook_bridge`` and ``codex_hook_bridge``.

Assumptions: descriptor files are local trusted install artifacts, while hook stdout,
stderr, exit codes, and timeouts are untrusted runtime results resolved by the v1
fail policy.
"""

from __future__ import annotations

from .runner import HOOK_API, V1_BLOCK_EXIT_CODE, load_descriptors, run_hook

__all__ = ["HOOK_API", "V1_BLOCK_EXIT_CODE", "load_descriptors", "run_hook"]
