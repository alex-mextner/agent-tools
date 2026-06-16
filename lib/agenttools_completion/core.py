r"""Core implementation of the universal zsh tab-completion generator + installer.

The public surface (``generate_zsh``, ``install``, ``uninstall``, ``status``,
``InstallResult``, ``StatusResult``) is re-exported from the package ``__init__``; import
from there, not from this module.

Design notes
------------
* **One source of truth: the argparse parser tree.** Hand-written ``#compdef`` files drift
  the moment a flag is added. Instead we introspect the *same* parser the CLI already
  builds — ``parser._actions`` for options/positionals, ``_SubParsersAction.choices`` for
  subcommands (recursively, so ``demo config get`` completes), ``action.choices`` for
  enumerated values, and the subparser ``.help`` / ``.description`` for descriptions. The
  completion can never lie about flags the tool actually accepts, because it is generated
  from those flags.

* **We read argparse "private" attributes on purpose.** ``parser._actions`` and
  ``_SubParsersAction.choices`` / ``_choices_actions`` are not public API, but they are
  stable across every supported CPython (3.9–3.13) and are the *only* way to walk a parser
  without re-running it. argparse exposes no public introspection. We touch only these few,
  well-known attributes and degrade gracefully (``getattr(..., default)``) if a future
  release renames one — a missing attribute yields a thinner completion, never a crash.

* **zsh-safety is the whole game.** The generated file is executed by zsh; a single
  unescaped ``:`` or ``'`` in a help string turns a valid ``_arguments`` spec into a syntax
  error and ``zsh -n`` fails. So:
    - Descriptions are collapsed to one line (newlines / runs of whitespace -> single
      space) and every zsh-special character in them is escaped via :func:`_zsh_desc`:
      ``:`` -> ``\:`` (it is the spec field separator), and the whole description is carried
      in a single-quoted zsh string with embedded single-quotes rendered as the ``'\''``
      idiom. Backticks / ``$`` / brackets are inert inside single quotes, so single-quoting
      neutralizes them without per-character escaping.
    - ``argparse.SUPPRESS`` help (the literal ``"==SUPPRESS=="``) becomes an EMPTY
      description — never the literal, which would both leak internals and (containing
      ``=``) confuse the spec.
  The generator's correctness is pinned by a test that runs ``zsh -n`` on the real output;
  if escaping regresses, that test goes red.

* **State-machine dispatch.** Top-level uses ``_arguments -C`` with ``'1: :->cmds'`` and
  ``'*::arg:->args'``; ``case $state`` then either offers the subcommand list (via
  ``_describe``) or, in the ``args`` state, switches on ``$line[1]`` (the *parsed* first
  positional, i.e. the chosen subcommand — not ``$words[1]``, which shifts when a global
  flag precedes the subcommand) to a per-subcommand helper ``_<prog>_<cmd>``. Nested
  subparsers recurse the same way (``_<prog>_<cmd>_<subcmd>``). Each helper is a real zsh
  function emitted into the file, so arbitrarily deep command trees complete correctly.

* **Installer is filesystem-only and injectable.** ``comp_dir`` and ``zshrc`` are
  parameters (defaulting to ``~/.zsh/completions`` and ``~/.zshrc``) so tests drive a temp
  dir and never touch the real shell config. The ``~/.zshrc`` edit lives between sentinel
  lines (``# >>> agent-tools completions >>>`` / ``# <<< ... <<<``); install rewrites that
  block in place (idempotent — re-running never duplicates it), uninstall removes the file
  and drops the block only when no other managed ``_*`` file remains in ``comp_dir``.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Sentinel lines bracketing our managed block in ~/.zshrc. Anything between them is ours to
# rewrite/remove; everything outside is the user's and is never touched.
_BEGIN = "# >>> agent-tools completions >>>"
_END = "# <<< agent-tools completions <<<"

# A *well-formed* managed block: BEGIN..END in order, non-greedily, with an optional trailing
# newline. Captures the inner body in group 1 so we can read the fpath dir it points at. One
# compiled regex is the single source of truth for "is a block present?" / "strip the block".
_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r"\n(?P<body>.*?)" + re.escape(_END) + r"\n?",
    re.DOTALL,
)

_DEFAULT_COMP_DIR = Path("~/.zsh/completions")
_DEFAULT_ZSHRC = Path("~/.zshrc")

_WS = re.compile(r"\s+")


# =============================================================================== escaping


def _one_line(text: str) -> str:
    """Collapse all whitespace (newlines, tabs, runs of spaces) to single spaces, trimmed.

    Help strings are often multi-line; a description in a zsh spec must be one line."""
    return _WS.sub(" ", text).strip()


def _clean_help(help_text: Optional[str]) -> str:
    """Turn an argparse ``help`` into a one-line description, mapping SUPPRESS -> "".

    ``argparse.SUPPRESS`` is the sentinel string ``"==SUPPRESS=="``; emitting it would both
    leak an internal marker and (it contains ``=``) risk confusing a spec. A suppressed help
    means "no description"."""
    if help_text is None or help_text == argparse.SUPPRESS:
        return ""
    return _one_line(help_text)


def _zsh_sq(text: str) -> str:
    """Wrap ``text`` in a zsh single-quoted string, escaping embedded single-quotes.

    Inside single quotes zsh treats everything literally — ``$``, backticks, brackets,
    parens are all inert — so the only character we must handle is the single-quote itself,
    via the classic ``'\\''`` idiom (close quote, escaped quote, reopen quote)."""
    return "'" + text.replace("'", "'\\''") + "'"


def _zsh_desc(help_text: Optional[str]) -> str:
    """Escape a help string for use as the description after the ``:`` in an _arguments spec.

    Two layers: (1) the colon is the spec field separator, so a literal ``:`` in the text is
    backslash-escaped to ``\\:``; (2) the result is then single-quoted (so the spec, which we
    wrap in single quotes, carries it safely). Backslash itself is escaped first so we don't
    double-process an escape we just introduced."""
    cleaned = _clean_help(help_text)
    # Escape backslashes first, then the spec-significant colon and the bracket that opens an
    # optional action message. These would otherwise be interpreted by _arguments.
    cleaned = cleaned.replace("\\", "\\\\").replace(":", "\\:").replace("[", "\\[")
    return cleaned


def _zsh_value(value: str) -> str:
    """Escape a single completion value (a choice) for a zsh ``(a b c)`` value group.

    Values sit inside parentheses separated by spaces; a value containing whitespace or a
    paren/colon/quote would break the group. We backslash-escape those few characters. Most
    choices are plain identifiers and pass through untouched."""
    return re.sub(r"([ \t():'\"\\])", r"\\\1", value)


# ============================================================================ introspection


def _is_subparsers(action: argparse.Action) -> bool:
    return isinstance(action, argparse._SubParsersAction)


def _subparser_help(action: argparse._SubParsersAction) -> Dict[str, str]:
    """Map subcommand name -> its description, from the _SubParsersAction's pseudo-actions.

    argparse stores per-choice help on ``_choices_actions`` (``_ChoicesPseudoAction``
    objects with ``.dest`` == the choice name and ``.help`` == the ``help=`` string). We
    read that, falling back to the subparser's own ``.description`` when no ``help=`` was
    given — so a subparser built with ``description=`` but no ``help=`` still gets a label."""
    by_name: Dict[str, str] = {}
    for pseudo in getattr(action, "_choices_actions", []):
        name = getattr(pseudo, "dest", None) or getattr(pseudo, "metavar", None)
        if name:
            by_name[name] = _clean_help(getattr(pseudo, "help", None))
    # Fill any name still lacking a description from the subparser's .description.
    for name, child in action.choices.items():
        if not by_name.get(name):
            by_name[name] = _clean_help(getattr(child, "description", None))
    return by_name


def _takes_value(action: argparse.Action) -> bool:
    """Does this optional action consume an argument value (``--config PATH``)?

    A flag (``store_true``/``store_false``/``store_const``/``count``/``help``/``version``)
    has ``nargs == 0``; those never take a value. Everything else (``store``, ``append``,
    ``extend``, ``append_const`` with a value, etc.) does. argparse encodes exactly this in
    ``action.nargs``: ``0`` means "no argument". ``None`` means "exactly one" (the default
    for ``store``), and any other value (``"?"``, ``"+"``, an int) also consumes at least one
    word. So "takes a value" == ``nargs != 0``."""
    return action.nargs != 0


def _iter_options(
    parser: argparse.ArgumentParser,
) -> List[Tuple[List[str], str, Optional[List[str]], bool]]:
    """Yield ``(option_strings, description, choices, takes_value)`` for every optional action.

    ``option_strings`` is e.g. ``["--mode"]`` or ``["--verbose", "-v"]``; ``choices`` is the
    action's ``choices`` as a list of strings, or ``None``; ``takes_value`` says whether the
    option consumes an argument (so the spec should request a completion after it).
    Positionals (no option strings) and the subparsers action are skipped here — they are
    handled separately."""
    out: List[Tuple[List[str], str, Optional[List[str]], bool]] = []
    for action in parser._actions:
        if _is_subparsers(action):
            continue
        if not action.option_strings:
            continue  # positional
        desc = _clean_help(action.help)
        choices = [str(c) for c in action.choices] if action.choices else None
        out.append((list(action.option_strings), desc, choices, _takes_value(action)))
    return out


def _positional_choices(parser: argparse.ArgumentParser) -> List[List[str]]:
    """Return the ``choices`` lists of any positional args that have them (in order).

    A positional with ``choices=[...]`` (e.g. ``lang`` in ``demo greet``) should offer those
    values. We collect them so the per-command helper can emit a ``_values`` group."""
    out: List[List[str]] = []
    for action in parser._actions:
        if _is_subparsers(action):
            continue
        if action.option_strings:
            continue  # optional, handled elsewhere
        if action.choices:
            out.append([str(c) for c in action.choices])
    return out


def _subparsers_action(parser: argparse.ArgumentParser) -> Optional[argparse._SubParsersAction]:
    for action in parser._actions:
        if _is_subparsers(action):
            return action
    return None


# ============================================================================== generation


def _func_name(prog: str, path: List[str]) -> str:
    """zsh function name for a command path: ``_demo`` / ``_demo_config`` / ``_demo_config_get``.

    Non-identifier characters in command names (rare, e.g. a hyphen) become underscores so
    the emitted function name is a valid zsh identifier."""
    parts = [prog, *path]
    return "_" + "_".join(re.sub(r"[^A-Za-z0-9]", "_", p) for p in parts)


def _option_specs(parser: argparse.ArgumentParser) -> List[str]:
    """Build the ``_arguments`` spec strings for this parser's options.

    Each spec is a single-quoted zsh string of the form ``--opt[desc]`` (a no-arg flag),
    ``--opt[desc]:msg:(a b c)`` (an option whose value is one of fixed ``choices``), or
    ``--opt[desc]:msg:_files`` (an option that takes a value with no enumerated choices —
    we fall back to file completion, the most useful default). When an option has multiple
    strings (``--verbose -v``) they are emitted as a ``{--verbose,-v}`` group so both
    complete to the same description; the brace expansion stays OUTSIDE the quotes so zsh
    expands it, with the spec body as its own single-quoted string concatenated on."""
    specs: List[str] = []
    for opt_strings, desc, choices, takes_value in _iter_options(parser):
        desc_esc = _zsh_desc(desc)
        head = opt_strings[0] if len(opt_strings) == 1 else "{" + ",".join(opt_strings) + "}"
        # The message between the colons names what's being completed: the description if we
        # have one, else a generic "value" — never empty (an empty :msg: reads oddly).
        msg = desc_esc or "value"
        if choices:
            values = " ".join(_zsh_value(c) for c in choices)
            body = f"[{desc_esc}]:{msg}:({values})"
        elif takes_value:
            body = f"[{desc_esc}]:{msg}:_files"
        else:
            body = f"[{desc_esc}]"
        if len(opt_strings) == 1:
            specs.append(_zsh_sq(head + body))
        else:
            specs.append(head + _zsh_sq(body))
    return specs


def _emit_leaf(prog: str, path: List[str], parser: argparse.ArgumentParser) -> str:
    """Emit a helper function for a command with NO further subcommands (a leaf).

    It runs ``_arguments`` over the command's options and, if it has positionals with
    choices, offers those values via ``_values``."""
    fname = _func_name(prog, path)
    lines = [f"{fname}() {{"]
    opt_specs = _option_specs(parser)
    pos_choices = _positional_choices(parser)

    if opt_specs:
        args_parts = ["_arguments -s", *opt_specs]
        lines.append("  " + " \\\n    ".join(args_parts))
    # Offer positional choices (if any) as a values group after the options.
    for choices in pos_choices:
        values = " ".join(_zsh_value(c) for c in choices)
        lines.append(f"  _values 'value' {values}")
    lines.append("}")
    return "\n".join(lines)


def _emit_node(prog: str, path: List[str], parser: argparse.ArgumentParser, out: List[str]) -> None:
    """Recursively emit the helper function for ``parser`` at command ``path``.

    A node WITH subcommands gets a state-machine ``_arguments -C`` that, in the ``cmds``
    state, ``_describe``s the subcommand list and, in the ``args`` state, dispatches on
    ``$line[1]`` (the parsed subcommand) to the child helper. A leaf is options only.
    Children are emitted first (so definitions precede use is irrelevant in zsh, but it
    keeps related functions together)."""
    sub = _subparsers_action(parser)
    if sub is None:
        out.append(_emit_leaf(prog, path, parser))
        return

    choices: Dict[str, argparse.ArgumentParser] = dict(sub.choices)
    helps = _subparser_help(sub)
    fname = _func_name(prog, path)

    # Emit each child helper first.
    for name, child in choices.items():
        _emit_node(prog, path + [name], child, out)

    # Build the _describe command list: "name:description" entries, each single-quoted.
    # The description must be colon-escaped (_describe splits each entry on the FIRST ':'),
    # same hazard as option specs — a help like "stats: show numbers" would otherwise split
    # at the wrong colon.
    describe_entries = []
    for name in choices:
        desc = _zsh_desc(helps.get(name, ""))
        describe_entries.append(_zsh_sq(f"{name}:{desc}" if desc else name))
    describe_list = " ".join(describe_entries)

    # Dispatch case: switch on $line[1] — the first parsed positional, which `_arguments -C`
    # populates with the chosen subcommand. ($words[1] is the command word itself / shifts
    # with global options, so it misroutes when a flag precedes the subcommand.)
    case_lines = ["    case $line[1] in"]
    for name in choices:
        child_fn = _func_name(prog, path + [name])
        # Pattern uses the raw command name; command names are identifiers/hyphens, safe.
        case_lines.append(f"      {_zsh_value(name)})")
        case_lines.append(f"        {child_fn}")
        case_lines.append("        ;;")
    case_lines.append("    esac")

    opt_specs = _option_specs(parser)
    args_parts = ["_arguments -C"]
    args_parts.extend(opt_specs)
    args_parts.append("'1: :->cmds'")
    args_parts.append("'*::arg:->args'")
    args_join = " \\\n    ".join(args_parts)

    fn = [
        f"{fname}() {{",
        "  local curcontext=\"$curcontext\" state line",
        f"  {args_join}",
        "  case $state in",
        "    cmds)",
        f"      local -a _subcmds=({describe_list})",
        "      _describe -t commands 'command' _subcmds",
        "      ;;",
        "    args)",
    ]
    fn.extend(case_lines)
    fn.extend([
        "      ;;",
        "  esac",
        "}",
    ])
    out.append("\n".join(fn))


def generate_zsh(parser: argparse.ArgumentParser, prog: str) -> str:
    """Generate a complete ``#compdef`` zsh completion script for ``parser`` as ``prog``.

    Walks the parser tree (subcommands recursively, options, choices, positional choices)
    and emits a syntactically-valid zsh completion script: a ``#compdef <prog>`` tag line,
    one helper function per command node (state-machine dispatch for nodes with
    subcommands, plain ``_arguments`` for leaves), and a trailing call to the top-level
    function. Every description and value is escaped for zsh, so the output passes
    ``zsh -n``. ``argparse.SUPPRESS`` help is rendered as an empty description, never the
    literal ``==SUPPRESS==``.
    """
    fname = _func_name(prog, [])
    parts: List[str] = []
    _emit_node(prog, [], parser, parts)

    header = (
        f"#compdef {prog}\n"
        f"# Auto-generated by agenttools_completion — do not edit by hand.\n"
        f"# Regenerate with the owning tool's completion command.\n"
    )
    body = "\n\n".join(parts)
    # zsh #compdef convention: a script loaded by name should call its entry function so
    # that `#compdef`-style autoloading drives completion directly.
    footer = f"\n\n{fname} \"$@\"\n"
    return header + "\n" + body + footer


# =============================================================================== installer


@dataclass(frozen=True)
class InstallResult:
    """Outcome of :func:`install`, carrying everything a CLI needs to print a clear report."""

    prog: str
    comp_file: Path
    zshrc: Path
    file_written: bool
    snippet_added: bool
    human: str


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of :func:`uninstall`. Distinct from :class:`InstallResult` so the field names
    read correctly for a removal (``file_removed`` / ``snippet_removed``, not ``…_written``
    / ``…_added``)."""

    prog: str
    comp_file: Path
    zshrc: Path
    file_removed: bool
    snippet_removed: bool
    human: str


@dataclass(frozen=True)
class StatusResult:
    """Outcome of :func:`status`: is the completion file present, is the dir on fpath?"""

    prog: str
    comp_file: Path
    zshrc: Path
    file_present: bool
    fpath_configured: bool
    human: str

    @property
    def installed(self) -> bool:
        """Fully installed == the file exists AND our fpath snippet is in place."""
        return self.file_present and self.fpath_configured


def _resolve(path: Path) -> Path:
    """Expand ``~`` and make absolute, without resolving symlinks (keeps test paths intact)."""
    return Path(os.path.expanduser(str(path)))


def _comp_dir(comp_dir: Optional[Path]) -> Path:
    return _resolve(comp_dir if comp_dir is not None else _DEFAULT_COMP_DIR)


def _zshrc_path(zshrc: Optional[Path]) -> Path:
    return _resolve(zshrc if zshrc is not None else _DEFAULT_ZSHRC)


def _snippet(comp_dir: Path) -> str:
    """The guarded fpath+compinit block written between the sentinels.

    We prepend ``comp_dir`` to ``fpath`` and ensure ``compinit`` runs. ``compinit`` is
    guarded so we don't force a second run if the user already calls it: we only autoload +
    run it when the ``compinit`` function/command isn't already defined. If the user's own
    ``compinit`` runs *after* this block, it picks up our fpath entry anyway — either way the
    completion loads."""
    return (
        f"{_BEGIN}\n"
        f"# Managed by agent-tools (agenttools_completion). Edits inside this block are\n"
        f"# overwritten on the next `install`. Remove via the tool's `completion uninstall`.\n"
        f'fpath=("{comp_dir}" $fpath)\n'
        f"if (( ! $+functions[compinit] )); then\n"
        f"  autoload -Uz compinit && compinit\n"
        f"fi\n"
        f"{_END}\n"
    )


def _strip_block(text: str) -> str:
    """Remove our sentinel-delimited block (and a single trailing blank line) from ``text``.

    Idempotent: if no well-formed block is present, returns ``text`` unchanged."""
    stripped = _BLOCK_RE.sub("", text)
    # Collapse a doubled blank line the removal may have left behind.
    return re.sub(r"\n{3,}", "\n\n", stripped)


def _write_zshrc_block(zshrc: Path, comp_dir: Path) -> bool:
    """Ensure exactly one up-to-date snippet block is present in ``zshrc``.

    Returns True if the file was modified. Reads the current content (empty if absent),
    strips any existing block, then appends a freshly-built one — so re-running with a
    different ``comp_dir`` rewrites in place and never duplicates."""
    existing = zshrc.read_text() if zshrc.exists() else ""
    without = _strip_block(existing)
    if without and not without.endswith("\n"):
        without += "\n"
    new = without + _snippet(comp_dir)
    if new == existing:
        return False
    zshrc.parent.mkdir(parents=True, exist_ok=True)
    zshrc.write_text(new)
    return True


def _has_block(zshrc: Path, comp_dir: Optional[Path] = None) -> bool:
    """Is a well-formed managed block present in ``zshrc``?

    With ``comp_dir`` given, also require the block's ``fpath=`` line to reference *that*
    directory — so ``status``/``uninstall`` for a given comp_dir never mistake an unrelated
    managed block (pointing elsewhere) for this one. This matters because ``comp_dir`` is
    injectable; without the path check, status could report installed against the wrong dir
    and uninstall could strip someone else's block."""
    if not zshrc.exists():
        return False
    match = _BLOCK_RE.search(zshrc.read_text())
    if match is None:
        return False
    if comp_dir is None:
        return True
    return f'"{comp_dir}"' in match.group("body")


def install(
    prog: str,
    script: str,
    *,
    comp_dir: Optional[Path] = None,
    zshrc: Optional[Path] = None,
) -> InstallResult:
    """Write ``script`` as ``<comp_dir>/_<prog>`` and ensure the fpath+compinit snippet.

    Idempotent: re-running overwrites the file and rewrites (never duplicates) the zshrc
    block. The completion dir is created if missing; the file is written 0644. ``comp_dir``
    and ``zshrc`` are injectable (default ``~/.zsh/completions`` and ``~/.zshrc``) so callers
    and tests can target a temp location."""
    cdir = _comp_dir(comp_dir)
    rc = _zshrc_path(zshrc)
    cdir.mkdir(parents=True, exist_ok=True)
    comp_file = cdir / f"_{prog}"

    file_written = (not comp_file.exists()) or comp_file.read_text() != script
    if file_written:
        comp_file.write_text(script)
    os.chmod(comp_file, 0o644)

    snippet_added = _write_zshrc_block(rc, cdir)

    human = (
        f"✓ installed completion to {comp_file}\n"
        f"{'✓ added' if snippet_added else '✓ fpath snippet already present in'} "
        f"{rc} — run `exec zsh` (or restart your shell) to activate."
    )
    return InstallResult(
        prog=prog,
        comp_file=comp_file,
        zshrc=rc,
        file_written=file_written,
        snippet_added=snippet_added,
        human=human,
    )


def uninstall(
    prog: str,
    *,
    comp_dir: Optional[Path] = None,
    zshrc: Optional[Path] = None,
) -> UninstallResult:
    """Remove ``<comp_dir>/_<prog>``; drop the zshrc block only when no managed file remains.

    Idempotent: removing an already-absent file or block is a no-op (never raises). The
    zshrc snippet is kept as long as *any* ``_*`` completion file remains in ``comp_dir``
    (other tools may rely on the fpath entry); once the dir has no managed completions left,
    the block is stripped."""
    cdir = _comp_dir(comp_dir)
    rc = _zshrc_path(zshrc)
    comp_file = cdir / f"_{prog}"

    file_removed = comp_file.exists()
    if file_removed:
        comp_file.unlink()

    remaining = sorted(p.name for p in cdir.glob("_*")) if cdir.exists() else []
    snippet_removed = False
    # Only strip the block when no managed completion remains in THIS dir AND the block
    # actually points at this dir (never strip an unrelated block that targets elsewhere).
    if not remaining and _has_block(rc, cdir):
        rc.write_text(_strip_block(rc.read_text()))
        snippet_removed = True

    if file_removed:
        human = f"✓ removed completion {comp_file}"
        if snippet_removed:
            human += f"\n✓ removed fpath snippet from {rc} (no completions remain)"
        elif remaining:
            human += f"\n  kept fpath snippet in {rc} ({len(remaining)} completion(s) remain: {', '.join(remaining)})"
    else:
        human = f"nothing to remove — {comp_file} was not present"

    return UninstallResult(
        prog=prog,
        comp_file=comp_file,
        zshrc=rc,
        file_removed=file_removed,
        snippet_removed=snippet_removed,
        human=human,
    )


def status(
    prog: str,
    *,
    comp_dir: Optional[Path] = None,
    zshrc: Optional[Path] = None,
) -> StatusResult:
    """Report whether ``_<prog>`` is present and whether our fpath snippet is in ``zshrc``."""
    cdir = _comp_dir(comp_dir)
    rc = _zshrc_path(zshrc)
    comp_file = cdir / f"_{prog}"

    file_present = comp_file.exists()
    # fpath is "configured for this tool" only if the managed block points at THIS comp_dir.
    fpath_configured = _has_block(rc, cdir)

    if file_present and fpath_configured:
        human = f"✓ {prog}: completion installed at {comp_file}; fpath wired in {rc}."
    elif file_present and not fpath_configured:
        human = (
            f"~ {prog}: completion file present at {comp_file}, but {rc} has no fpath "
            f"snippet. Run install to wire it, or add the dir to fpath yourself."
        )
    elif not file_present and fpath_configured:
        human = (
            f"~ {prog}: fpath wired in {rc}, but no completion file at {comp_file}. "
            f"Run install to generate it."
        )
    else:
        human = f"✗ {prog}: not installed. Run the tool's `completion install` to enable tab-completion."

    return StatusResult(
        prog=prog,
        comp_file=comp_file,
        zshrc=rc,
        file_present=file_present,
        fpath_configured=fpath_configured,
        human=human,
    )
