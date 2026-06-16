"""``python3 -m cc_hook_bridge <event>`` — the entry point CC's settings.json invokes."""

from __future__ import annotations

import sys

from .dispatch import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
