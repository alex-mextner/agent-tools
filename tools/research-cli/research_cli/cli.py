"""cli — the self-registering command dispatcher for research-cli.

REACHED VIA ``bin/research`` -> :func:`main`. Commands self-register: every module in
``research_cli/commands/`` that exposes ``NAME``, ``SUMMARY`` and ``run(argv) -> int``
becomes a subcommand with ZERO edits here (self-registering-commands skill). Drop a file,
get a command.

IMPORT-CLEAN AT TOP (lazy-heavy-imports skill): this dispatcher imports only stdlib (the
shared error layer in ``._errors`` is itself stdlib-only), and each command module is
imported lazily when first dispatched, so ``research --help`` / ``research --version`` and an
unrelated command never pay for another command's heavy deps (or the providers import).

ERRORS (error-system v2): a diagnosed dispatcher failure is raised as a shared
``agenttools_errors`` structured error (WHAT / WHY / HOW-to-fix) and rendered through
:func:`guard`, so an unknown command gets the did-you-mean heuristic and a command-load
failure renders the same three-part block + a stable per-class exit code as the rest of the
ecosystem (rig / review / …) — not a bare ``print`` + an ad-hoc number.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__
from ._errors import EXIT_INTERNAL, AgentToolError, guard, unknown_item_error

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


def _dispatch(argv: List[str], commands: Dict[str, Tuple[str, str]]) -> int:
    """Route ``argv`` to a command, raising a structured :class:`AgentToolError` on a diagnosed
    failure (unknown command / load failure). Meta paths (help/version) print and return here.
    Wrapped by :func:`main` in :func:`guard`, which renders the error + maps it to its exit code.
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage(commands))
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"research {__version__}")
        return 0

    name, rest = argv[0], argv[1:]
    if name not in commands:
        # The shared builder gives the did-you-mean heuristic (suggest the nearest command) and
        # the 3-part block, instead of a bare "unknown command 'x'" string + a raw usage dump.
        raise unknown_item_error(category="command", bad=name, known=set(commands))

    mod_name, _ = commands[name]
    try:
        run = _load_run(mod_name)
    except Exception as exc:
        # A command that fails to IMPORT is a bug in the tool, not user usage — exit INTERNAL.
        raise AgentToolError(
            what=f"cannot load command {name!r}: {exc}",
            why=f"importing {mod_name} failed — the command module is broken",
            fix="file a bug; re-run another command, or reinstall research-cli",
            exit_code=EXIT_INTERNAL,
        ) from exc
    return int(run(rest))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = _discover()
    return guard(lambda: _dispatch(argv, commands))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
