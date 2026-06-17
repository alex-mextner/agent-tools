"""cli — the self-registering command dispatcher for research-cli.

REACHED VIA ``bin/research`` -> :func:`main`. Commands self-register: every module in
``research_cli/commands/`` that exposes ``NAME``, ``SUMMARY`` and ``run(argv) -> int``
becomes a subcommand with ZERO edits here (self-registering-commands skill). Drop a file,
get a command.

IMPORT-CLEAN AT TOP (lazy-heavy-imports skill): this dispatcher imports only stdlib, and
each command module is imported lazily when first dispatched, so ``research --help`` /
``research --version`` and an unrelated command never pay for another command's heavy
deps (or the providers import).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__

# A command module's public contract: NAME (str), SUMMARY (str), run(argv) -> int.
_RunFn = Callable[[List[str]], int]


def _discover() -> Dict[str, Tuple[str, str]]:
    """Map command NAME -> (module_name, SUMMARY) by scanning the commands package.

    Only the lightweight NAME/SUMMARY are read here (the module is imported but its
    ``run`` is not called); a malformed command module is skipped with a warning rather
    than breaking the whole CLI.
    """
    from . import commands as commands_pkg

    found: Dict[str, Tuple[str, str]] = {}
    for info in pkgutil.iter_modules(commands_pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod_name = f"{commands_pkg.__name__}.{info.name}"
        try:
            mod = importlib.import_module(mod_name)
            name = getattr(mod, "NAME")
            summary = getattr(mod, "SUMMARY", "")
            getattr(mod, "run")  # presence check; not called
        except Exception as exc:  # a broken command must not kill the dispatcher
            print(f"research: skipping command module {mod_name}: {exc}", file=sys.stderr)
            continue
        found[name] = (mod_name, summary)
    return found


def _load_run(mod_name: str) -> _RunFn:
    return getattr(importlib.import_module(mod_name), "run")


def _usage(commands: Dict[str, Tuple[str, str]]) -> str:
    lines = [
        "research — multi-provider research/panel on the agent-tools providers engine",
        "",
        "Usage: research <command> [options]",
        "",
        "Commands:",
    ]
    width = max((len(n) for n in commands), default=0)
    for name in sorted(commands):
        _, summary = commands[name]
        lines.append(f"  {name.ljust(width)}  {summary}")
    lines += [
        "",
        "Global:",
        "  -h, --help        show this help",
        "  -V, --version     show version",
        "",
        "Run `research <command> --help` for command options.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = _discover()

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage(commands))
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"research {__version__}")
        return 0

    name, rest = argv[0], argv[1:]
    if name not in commands:
        print(f"research: unknown command {name!r}\n", file=sys.stderr)
        print(_usage(commands), file=sys.stderr)
        return 2  # usage error

    mod_name, _ = commands[name]
    try:
        run = _load_run(mod_name)
    except Exception as exc:
        print(f"research: cannot load command {name!r}: {exc}", file=sys.stderr)
        return 70  # internal software error
    return int(run(rest))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
