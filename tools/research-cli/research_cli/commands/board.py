"""board — show the research failover board, resolved against the shared manifest.

A diagnostic / introspection command: it resolves each research seat through the shared
providers registry (``resolve_role``) and prints the priority-ordered board with the
concrete model each lens currently resolves to, its provider, and its capability tags.
Useful to confirm the providers-engine reuse is wired and to see what a manifest bump
changed — without making any network call.

EXIT CODES: 0 ok; 70 EX_SOFTWARE on a malformed/unresolvable manifest.
"""

from __future__ import annotations

import argparse
import sys
from typing import List

NAME = "board"
SUMMARY = "show the research board resolved against the shared model manifest"

_EX_SOFTWARE = 70


def run(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="research board", description=SUMMARY)
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="path to a models.yaml manifest (default: the ecosystem manifest)",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed (help -> 0, error -> 2)
        return int(exc.code) if exc.code is not None else 2

    from pathlib import Path

    from ..providers import (
        ProviderError,
        load_research_registry,
        research_board,
        resolve_seat,
    )

    manifest = Path(args.manifest) if args.manifest else None
    try:
        registry = load_research_registry(manifest)
    except ProviderError as exc:
        print(f"research board: {exc}", file=sys.stderr)
        return _EX_SOFTWARE

    board = research_board()
    print("Research board (priority order, strongest first):\n")
    for i, seat in enumerate(board.seats, start=1):
        try:
            entry = resolve_seat(registry, seat)
            caps = ", ".join(sorted(entry.capabilities)) or "—"
            print(
                f"  {i}. {seat.display:<10} lens={seat.role:<9} "
                f"-> {entry.id}  [{entry.provider}]  ({caps})"
            )
        except ProviderError as exc:
            print(f"  {i}. {seat.display:<10} lens={seat.role:<9} -> UNRESOLVED: {exc}")
    return 0
