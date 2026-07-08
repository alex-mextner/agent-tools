"""``python -m agenttools_stall_watchdog`` entry point — delegates to :mod:`cli`."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
