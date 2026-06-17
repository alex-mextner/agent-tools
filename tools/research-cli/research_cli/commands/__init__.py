"""commands — the self-registering subcommand package.

Each module here exposing ``NAME`` / ``SUMMARY`` / ``run(argv) -> int`` is a research-cli
subcommand, discovered by ``research_cli.cli`` with no central registry to edit. Add a
command by adding a file; that is the whole contract (self-registering-commands skill).
"""

from __future__ import annotations
