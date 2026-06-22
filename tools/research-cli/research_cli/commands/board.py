"""board — show the research failover board, resolved against the shared manifest.

A diagnostic / introspection command: it resolves each research seat through the shared
providers registry (``resolve_role``) and prints the priority-ordered board with the
concrete model each lens currently resolves to, its provider, and its capability tags.
Useful to confirm the providers-engine reuse is wired and to see what a manifest bump
changed — without making any network call.

EXIT CODES (shared agenttools_errors contract — error-system v2):
  0   ok                                                            EXIT_OK
  2   a malformed/missing/unresolvable manifest (the usage/config class, was the ad-hoc 70)
                                                                    EXIT_USAGE
"""

from __future__ import annotations

import argparse
from typing import List

from .._errors import EXIT_USAGE, UsageError, guard

NAME = "board"
SUMMARY = "show the research board resolved against the shared model manifest"


def _run(args: argparse.Namespace) -> int:
    """Resolve + print the board; raises a structured :class:`UsageError` on a bad manifest.
    :func:`run` wraps this in :func:`guard` to render it + map it to its exit code."""
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
        # A bad/unresolvable manifest is bad INPUT (the usage/config class), not an internal
        # crash — so a caller branches on EXIT_USAGE, and the fix points at the manifest.
        raise UsageError(
            what=f"could not load the models manifest: {exc}",
            why="the manifest is missing, malformed, or names an unknown model",
            fix="check --manifest (or the default models.yaml), or omit it to use the default",
        ) from exc

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
        return int(exc.code) if exc.code is not None else EXIT_USAGE
    return guard(lambda: _run(args))
