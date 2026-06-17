"""agenttools_completion — a universal zsh tab-completion generator + auto-installer.

Every CLI in the ecosystem wants shell tab-completion, and every CLI that ships a
hand-written ``#compdef`` file watches it rot the moment a flag changes. This module
generates the completion script straight from the tool's own ``argparse`` parser — the same
parser ``--help`` is built from — so the completion can never disagree with the flags the
tool actually accepts. Plus an idempotent installer that drops the script into an ``fpath``
dir and wires ``~/.zshrc`` once.

What it does
------------
* **Generate** (:func:`generate_zsh`) — introspect an ``argparse.ArgumentParser`` tree and
  emit a valid ``#compdef`` zsh script: subcommands (recursively, so nested ``rig stats
  show`` completes), per-command options (long ``--foo`` + short ``-f``) with help as the
  description, ``choices=[...]`` on options *and* positionals offered as completion values,
  and ``argparse.SUPPRESS`` help rendered as an empty description (never the literal
  ``==SUPPRESS==``). Every description/value is escaped so the output passes ``zsh -n``.
* **Install / uninstall / status** (:func:`install`, :func:`uninstall`, :func:`status`) —
  write ``<comp_dir>/_<prog>`` (default ``~/.zsh/completions``, mode 0644), append a
  guarded, idempotent fpath+compinit block to ``~/.zshrc`` between sentinel lines, and
  report state. ``comp_dir`` / ``zshrc`` are injectable so tests never touch the real shell
  config. Uninstall is idempotent and keeps the fpath snippet while other managed
  completions remain.

Why stdlib only (no ``argcomplete``, no ``shtab``)
--------------------------------------------------
The ecosystem is stdlib-first by directive. ``argcomplete`` is a *runtime* completer that
needs a hook in the shell and re-invokes your Python on every Tab — heavier and slower than
a static ``#compdef`` file, and it doesn't emit a standalone script. ``shtab`` does emit
static scripts but is another dependency surface for what is, for zsh specifically, a few
hundred lines of argparse-walking + careful escaping we can own outright (and tie down with
a real ``zsh -n`` test). Owning it also lets the escaping be exactly as paranoid as zsh
demands.

Quick start
-----------
    import argparse
    from agenttools_completion import generate_zsh, install

    parser = build_parser()                      # your CLI's argparse parser
    script = generate_zsh(parser, "mytool")      # a complete #compdef _mytool script
    result = install("mytool", script)           # writes ~/.zsh/completions/_mytool + zshrc
    print(result.human)                          # "✓ installed completion to ... run `exec zsh`"

See ``lib/agenttools_completion/README.md`` for the full reference, the generated-script
shape, and the recommended per-tool ``<tool> completion install|uninstall|status`` wiring.
"""

from __future__ import annotations

from .core import (
    InstallResult,
    StatusResult,
    UninstallResult,
    generate_zsh,
    install,
    status,
    uninstall,
)

__all__ = [
    "InstallResult",
    "StatusResult",
    "UninstallResult",
    "generate_zsh",
    "install",
    "status",
    "uninstall",
]

__version__ = "0.1.0"
