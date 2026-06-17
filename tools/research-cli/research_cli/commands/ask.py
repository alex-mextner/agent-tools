"""ask — run a single-round multi-provider research pass on a question.

The MVP command: fan a question out to the research Board's reachable seats (reusing the
shared providers engine for board / failover / key cascade), then print the synthesized
panel note. Network access goes through the transport; ``--offline`` swaps in the stub
transport so the command runs end-to-end with no key (useful for a demo, CI, or a dry
run of the wiring).

EXIT CODES (structured-exit-codes skill):
  0   a synthesis was produced (at least one seat answered)
  2   usage error (no question, bad flag)
  69  EX_UNAVAILABLE — no seat was reachable / answered (e.g. no key, no backend)
  70  EX_SOFTWARE   — an internal/config error (e.g. a malformed manifest)
"""

from __future__ import annotations

import argparse
import sys
from typing import List

NAME = "ask"
SUMMARY = "run a single-round multi-provider research pass on a question"

_EX_USAGE = 2
_EX_UNAVAILABLE = 69
_EX_SOFTWARE = 70


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="research ask",
        description=SUMMARY,
    )
    p.add_argument("question", nargs="+", help="the research question (quote it)")
    p.add_argument(
        "--pool",
        type=int,
        default=3,
        metavar="N",
        help="how many reachable seats to ask (default: 3; <=0 means all reachable)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECS",
        help="per-seat call timeout in seconds (default: 120)",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="use the stub transport (no network) — for demos / CI / wiring checks",
    )
    p.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="path to a models.yaml manifest (default: the ecosystem manifest)",
    )
    return p


def run(argv: List[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the message (help -> 0, error -> 2)
        return int(exc.code) if exc.code is not None else _EX_USAGE

    question = " ".join(args.question).strip()
    if not question:
        print("research ask: empty question", file=sys.stderr)
        return _EX_USAGE

    # Lazy imports: the heavy providers/engine wiring is only paid for on a real run, so
    # `research --help` and discovery stay fast (lazy-heavy-imports skill).
    from pathlib import Path

    from ..engine import ResearchEngine
    from ..providers import ProviderError
    from ..transport import StubTransport, SubprocessTransport

    transport = StubTransport() if args.offline else SubprocessTransport()
    manifest = Path(args.manifest) if args.manifest else None

    engine = ResearchEngine(
        transport=transport,
        pool_size=args.pool,
        timeout=args.timeout,
        manifest_path=manifest,
    )

    try:
        result = engine.run(question)
    except ProviderError as exc:
        # A bad manifest / unresolvable board is a config (software) error, not "nothing
        # reachable" — distinguish so a script can tell a typo from an offline machine.
        print(f"research ask: {exc}", file=sys.stderr)
        return _EX_SOFTWARE
    except ValueError as exc:
        print(f"research ask: {exc}", file=sys.stderr)
        return _EX_USAGE

    print(result.synthesis)

    if not result.answered:
        print(
            "research ask: no seat answered — set a provider key (or "
            "RESEARCH_BACKEND_CMD), or use --offline.",
            file=sys.stderr,
        )
        return _EX_UNAVAILABLE
    return 0
