"""codex-hook-bridge - make agents-hooks/v1 descriptors fire in Codex.

Accessed via: Codex hook commands in ``~/.codex/config.toml`` invoke
``python3 -m codex_hook_bridge <Event>`` with the native Codex hook JSON on stdin.

Assumptions: Codex block output is the plain top-level
``{"decision":"block","reason":"..."}`` shape, and ``apply_patch`` carries patch text
in ``tool_input.command``.
"""

from __future__ import annotations

from . import dispatch

__all__ = ["dispatch"]
