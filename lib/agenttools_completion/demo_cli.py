"""demo_cli — a tiny standalone argparse CLI that proves the generator end-to-end.

This file is the always-present, dependency-free proof for ``agenttools_completion``:
``build_demo_parser()`` returns a parser exercising every feature the generator must
handle — top-level options, ``choices`` on an option, subcommands with their own options,
a positional with ``choices``, and a NESTED subparser (``demo config get``) — and the
``__main__`` block prints the generated ``_demo`` script so you can eyeball / ``zsh -n`` it::

    python -m agenttools_completion.demo_cli            # print the generated _demo script
    python -m agenttools_completion.demo_cli | zsh -n /dev/stdin && echo OK

It is intentionally NOT wired into any real tool — the real tools grow their own
``<tool> completion install`` later (see README "Wiring into a real CLI"). This is the
reference shape a consumer copies.
"""

from __future__ import annotations

import argparse


def build_demo_parser() -> argparse.ArgumentParser:
    """A demo parser covering options+choices, subcommands, a nested subparser, and a
    positional with choices — the full matrix the zsh generator introspects."""
    p = argparse.ArgumentParser(
        prog="demo",
        description="demo — a toy CLI that exists only to prove tab-completion generation.",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="be loud")
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="info",
        help="logging verbosity",
    )

    sub = p.add_subparsers(dest="command", metavar="<command>")

    greet = sub.add_parser("greet", help="greet someone")
    greet.add_argument("--shout", action="store_true", help="uppercase the greeting")
    greet.add_argument(
        "lang",
        nargs="?",
        choices=["en", "fr", "de"],
        help="language to greet in",
    )

    # A nested subparser: `demo config get|set`.
    config = sub.add_parser("config", help="read or write configuration")
    config_sub = config.add_subparsers(dest="config_command", metavar="<subcommand>")
    cfg_get = config_sub.add_parser("get", help="print a config value")
    cfg_get.add_argument("key", help="the config key to read")
    cfg_set = config_sub.add_parser("set", help="set a config value")
    cfg_set.add_argument("key", help="the config key to write")
    cfg_set.add_argument("value", help="the value to store")
    cfg_set.add_argument(
        "--scope",
        choices=["local", "global"],
        default="local",
        help="where to write the value",
    )

    return p


def main() -> None:
    """Print the generated _demo completion script to stdout (so it can be piped to zsh)."""
    from agenttools_completion import generate_zsh

    print(generate_zsh(build_demo_parser(), "demo"), end="")


if __name__ == "__main__":
    main()
