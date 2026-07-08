"""``python3 -m codex_hook_bridge <event>`` entry point for Codex hooks."""

from __future__ import annotations

import sys

from .dispatch import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
