"""`python -m advertise <tool> --skill-md FILE` — ad-hoc skill install/relink."""

from __future__ import annotations

import sys

from .core import _main

if __name__ == "__main__":
    sys.exit(_main())
