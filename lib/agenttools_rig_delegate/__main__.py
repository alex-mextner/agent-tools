"""CLI mirror of the :mod:`agenttools_rig_delegate` library, for non-Python CLIs.

tg-ctl is a bun/TS binary and cannot import the Python library, so it shells out here.
Keep this surface in lock-step with ``__init__.py``.

Usage
-----
    python3 -m agenttools_rig_delegate detect
        Exit 0 and print the resolved rig path if rig is present; exit 1 if absent.
        This is the "should I delegate?" decision for a shell caller.

    python3 -m agenttools_rig_delegate delegate [RIG_ARG ...]
        Run ``rig <RIG_ARG ...>`` and exit with rig's own exit code. If rig is absent,
        exit with the sentinel code 3 (distinct from rig's 0/1/2) so the caller knows to
        run its own fallback installer rather than treat it as a rig failure.
"""

from __future__ import annotations

import sys

from . import delegate as _delegate
from . import find_rig

# Exit code returned by ``delegate`` when rig is absent, so a shell caller can branch to
# its own fallback. Distinct from rig's typical 0/1/2 and from shell's 127.
NO_RIG_EXIT = 3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _usage()
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "detect":
        rig = find_rig()
        if rig is None:
            return 1
        print(rig)
        return 0
    if cmd == "delegate":
        rig = find_rig()
        if rig is None:
            return NO_RIG_EXIT
        return _delegate(rest).returncode
    _usage()
    return 2


def _usage() -> None:
    print(
        "usage: python3 -m agenttools_rig_delegate {detect | delegate [RIG_ARG ...]}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
