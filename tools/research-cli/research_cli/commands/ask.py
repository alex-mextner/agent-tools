"""ask — run a single-round multi-provider research pass on a question.

The MVP command: fan a question out to the research Board's reachable seats (reusing the
shared providers engine for board / failover / key cascade), then print the synthesized
panel note. Network access goes through the transport; ``--offline`` swaps in the stub
transport so the command runs end-to-end with no key (useful for a demo, CI, or a dry
run of the wiring).

EXIT CODES (shared agenttools_errors contract — error-system v2):
  0   a synthesis was produced (at least one seat answered)         EXIT_OK
  2   usage error: no question, a bad flag, OR a malformed/missing manifest (the config class)
                                                                    EXIT_USAGE
  7   no seat was reachable / answered (no key, no backend, offline) EXIT_NETWORK

  NOTE — contract migration: this command USED to return the ad-hoc ``69`` (EX_UNAVAILABLE)
  for "no seat answered" and ``70`` (EX_SOFTWARE) for "bad manifest". Both are replaced by the
  ecosystem-wide ``agenttools_errors`` codes so a caller branches on ONE contract across every
  tool: "nothing reachable" is the NETWORK class (7); a malformed manifest is the USAGE/config
  class (2, it's bad input, not an internal crash). A genuine internal bug propagates as the
  uncaught-exception exit 1 (EXIT_INTERNAL) via ``guard``.
"""

from __future__ import annotations

import argparse
from typing import List

from .._errors import EXIT_USAGE, NetworkError, UsageError, guard

NAME = "ask"
SUMMARY = "run a single-round multi-provider research pass on a question"


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
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable JSON object instead of the Markdown note",
    )
    p.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="path to a models.yaml manifest (default: the ecosystem manifest)",
    )
    return p


def _run(args: argparse.Namespace) -> int:
    """The real command body — raises a structured :class:`AgentToolError` on a diagnosed
    failure; :func:`run` wraps this in :func:`guard` to render it + map it to its exit code."""
    question = " ".join(args.question).strip()
    if not question:
        raise UsageError(
            what="empty question",
            why="`research ask` needs a question to research",
            fix='pass one (quote it): research ask "is X faster than Y?"',
        )

    # Lazy imports: the heavy providers/engine wiring is only paid for on a real run, so
    # `research --help` and discovery stay fast (lazy-heavy-imports skill).
    from pathlib import Path

    from ..engine import ResearchEngine, render_json
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
        # A bad/unresolvable manifest is bad INPUT (the usage/config class), not an internal
        # crash — so a caller branches on EXIT_USAGE, and the fix points at the manifest.
        raise UsageError(
            what=f"could not resolve the research board: {exc}",
            why="the models manifest is missing, malformed, or names an unknown model",
            fix="check --manifest (or the default models.yaml), or omit it to use the default",
        ) from exc
    except ValueError as exc:
        raise UsageError(
            what=str(exc),
            why="an argument value was invalid",
            fix="see `research ask --help` for the accepted values",
        ) from exc

    # The JSON object is printed BEFORE any error block, so a script gets the (possibly
    # empty) structured payload even when nothing answered — it branches on the exit code
    # AND reads the data. The Markdown note is the default human output.
    if args.as_json:
        print(render_json(result.question, list(result.answers)))
    else:
        print(result.synthesis)

    if not result.answered:
        # Nothing reachable answered — the NETWORK class (no key / no backend / offline stub
        # with no canned answer). Distinct from a bad manifest (usage) so a script can tell an
        # offline machine from a typo.
        raise NetworkError(
            what="no seat answered",
            why="no reachable provider returned an answer (no key, no backend, or offline)",
            fix="set a provider key (or RESEARCH_BACKEND_CMD), or run with --offline for a demo",
        )
    return 0


def run(argv: List[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the message (help -> 0, error -> 2)
        return int(exc.code) if exc.code is not None else EXIT_USAGE
    return guard(lambda: _run(args))
