"""agenttools_help.core — one shared help formatter for every agent-tools CLI.

WHAT THIS FILE IS
    The engine behind the ecosystem's ``--help`` / ``help <topic>`` output. ``tg``'s help was
    NOT colorized like ``review`` / ``rig`` / ``draw`` — inconsistent — and every tool
    re-implemented section layout, the usage line, and the subcommands list slightly
    differently. This is the ONE shared help layer the roadmap calls for: build it once, here,
    and the help-clarity rules ("show ACTUAL defaults, scope subcommand-only options to their
    subcommand, advertise topic-help, install-* state ✓/conflict") live in a single place so a
    fix lands across every CLI at once.

HOW IT'S REACHED AT RUNTIME
    A CLI builds a :class:`HelpFormatter` (prog + tagline), adds usage line(s), sections of
    options, a subcommands list, an install-state block, and an advertised list of help topics,
    then calls ``.render()`` for ``<tool> --help``. ``<tool> help <topic>`` resolves a
    :class:`Topic` from a :class:`TopicRegistry` and renders just that. Defaults are rendered
    from the ACTUAL runtime value (``opt(... default=resolve_model())``) so ``--model`` shows
    what will really be used, not a stale literal.

INVARIANTS / DESIGN
    - **Stdlib only at import time** (AGENTS.md hard rule): ``dataclasses`` / ``os`` / ``sys``
      / ``shutil`` / ``typing``. No third-party deps, so ``--help`` stays fast and offline.
    - **Color is opt-in by capability, never hard-coded.** ``NO_COLOR`` disables, ``FORCE_COLOR``
      forces, else color only on a real TTY — the same precedence as ``agenttools_errors`` so an
      error and the help printed around it look identical. The palette is one place: change the
      scheme here and the whole ecosystem matches.
    - **Layout is data, not strings.** A CLI describes its help as options/sections/subcommands
      (data); the formatter owns the rendering (alignment, wrapping, color). So the help-clarity
      rules are enforced structurally — e.g. a subcommand-scoped option physically can't leak
      into the global block because it lives under its subcommand's section.
    - **Install/setup state is first-class.** :func:`install_state_line` renders the
      ``✓ configured`` / ``○ not configured`` / ``⚠ conflict`` markers the roadmap wants on
      every install-/setup-/voice-/completion-state surface, so the user sees what's done vs
      pending without running it blindly.

History: born from the tg-cli help-coloring inconsistency + the same-day help-clarity rules
(actual defaults, scoped options, topic-help, install state). Extracted into the shared lib so
every CLI imports it instead of re-implementing help (the "extract everything reusable"
principle, §3 of the roadmap).
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ── color ───────────────────────────────────────────────────────────────────────────
# One palette for the whole ecosystem. ANSI SGR codes; semantic names so a CLI never hard-
# codes a number. Keep these in sync with agenttools_errors' render() (red/dim/green/yellow).
_PALETTE: Dict[str, str] = {
    "title": "1;36",  # bold cyan — prog name / banner
    "heading": "1",  # bold — section headings ("usage:", "options:", …)
    "option": "36",  # cyan — option/flag names (--model)
    "subcommand": "32",  # green — subcommand names
    "topic": "36",  # cyan — help-topic names
    "default": "2",  # dim — "(default: …)" annotations
    "dim": "2",  # dim — secondary text
    "ok": "32",  # green — ✓ configured
    "warn": "33",  # yellow — ○ not-configured / ⚠ conflict
    "err": "31",  # red — error/conflict markers
}

# install/setup state glyphs — the roadmap's ✓ / ○ / ⚠ vocabulary, one place.
GLYPH_OK = "✓"
GLYPH_PENDING = "○"
GLYPH_CONFLICT = "⚠"

# Left-column width caps (chars) before a row's description wraps to the next line. Options get
# a slightly wider cap than name/summary pairs because "-m, --model MODEL" is longer than a bare
# subcommand/topic name; both are capped so a single very long name can't push the column out.
_OPT_COL_CAP = 28
_PAIR_COL_CAP = 24


def _stream_is_tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except Exception:
        return False


# Values that mean "off" for FORCE_COLOR — matches the FORCE_COLOR=0 convention used by
# Node-style CLIs (chalk, supports-color), where 0/false disables even though the var is set.
_FORCE_COLOR_OFF = {"", "0", "false", "no", "off"}


def should_color(stream: object = None) -> bool:
    """Whether to emit ANSI color for ``stream`` (default stdout).

    ``NO_COLOR`` (any non-empty value) disables — the no-color.org standard (an *empty*
    ``NO_COLOR=""`` does NOT disable). ``FORCE_COLOR`` forces color on for a truthy value
    (``1``/``true``/…) even when piped; ``FORCE_COLOR=0`` (or ``false``/``no``/``off``) forces it
    OFF (the chalk/supports-color convention). Otherwise color only on a real TTY. Help prints to
    stdout, so the default stream is stdout — mirror of :func:`agenttools_errors.should_color`
    (which defaults to stderr).
    """
    if os.environ.get("NO_COLOR"):
        return False
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force.strip().lower() not in _FORCE_COLOR_OFF
    return _stream_is_tty(stream if stream is not None else sys.stdout)


class Palette:
    """A bound colorizer: ``p.option('--model')`` → the styled (or plain) string.

    Constructed with a single ``enabled`` flag (resolve it once with :func:`should_color`),
    so a whole render pass is consistent and there's no per-call env lookup. Unknown role names
    fall through to plain text rather than raising — a typo degrades to no-color, never crashes
    the help.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def paint(self, role: str, s: str) -> str:
        code = _PALETTE.get(role)
        if not self.enabled or code is None:
            return s
        return f"\033[{code}m{s}\033[0m"

    # convenience methods, one per semantic role used by the formatter
    def title(self, s: str) -> str:
        return self.paint("title", s)

    def heading(self, s: str) -> str:
        return self.paint("heading", s)

    def option(self, s: str) -> str:
        return self.paint("option", s)

    def subcommand(self, s: str) -> str:
        return self.paint("subcommand", s)

    def topic(self, s: str) -> str:
        return self.paint("topic", s)

    def default(self, s: str) -> str:
        return self.paint("default", s)

    def dim(self, s: str) -> str:
        return self.paint("dim", s)

    def ok(self, s: str) -> str:
        return self.paint("ok", s)

    def warn(self, s: str) -> str:
        return self.paint("warn", s)

    def err(self, s: str) -> str:
        return self.paint("err", s)


def _color_tail(chunk: str, chunk_offset: int, tail_start: int, p: Palette) -> str:
    """Dim-color the portion of ``chunk`` whose char positions are >= ``tail_start``.

    ``chunk`` is one wrapped line; ``chunk_offset`` is where it begins in the plain body;
    ``tail_start`` is where the annotation begins in the plain body. The slice of ``chunk`` that
    falls inside the annotation range is painted dim; the rest is left plain. This applies color
    AFTER wrapping, so an ANSI escape can never straddle a wrap boundary.
    """
    chunk_end = chunk_offset + len(chunk)
    if tail_start >= chunk_end:
        return chunk  # entirely description
    if tail_start <= chunk_offset:
        return p.default(chunk)  # entirely annotation
    split = tail_start - chunk_offset
    return chunk[:split] + p.default(chunk[split:])


# ── option / default rendering ────────────────────────────────────────────────────────
# Sentinel for "this option has NO default to show" — distinct from a real ``None``/``""``
# default, so a store_true switch doesn't render a misleading "(default: none)".
_UNSET = object()


@dataclass
class Option:
    """One option/flag in a help section.

    ``names`` — the spellings ("-m", "--model"); ``help`` — the one-line description; ``metavar``
    — the value placeholder ("MODEL") for value-taking options; ``default`` — the ACTUAL default
    to show (a literal, or — preferably — the resolved runtime value so ``--model`` reflects what
    will really run); ``choices`` — the allowed values, shown inline.

    Pass ``default=None`` for a flag that has no meaningful default (a store_true switch); pass
    an explicit ``default=""`` only if the empty string is genuinely the default.
    """

    names: Sequence[str]
    help: str = ""
    metavar: str = ""
    default: object = _UNSET  # sentinel: "no default to show"
    choices: Optional[Sequence[str]] = None

    def header(self) -> str:
        """The left column: ``-m, --model MODEL`` (no color — the formatter colors it)."""
        joined = ", ".join(self.names)
        if self.metavar:
            return f"{joined} {self.metavar}"
        return joined

    def annotations(self) -> str:
        """The trailing ``(choices: …) (default: …)`` text, or '' when neither applies.

        Shows ACTUAL defaults — the central help-clarity rule. ``default`` defaults to a private
        sentinel so we can distinguish "no default to show" from a real ``None``/``""`` default.
        """
        parts: List[str] = []
        if self.choices:
            parts.append(f"choices: {', '.join(str(c) for c in self.choices)}")
        if self.default is not _UNSET:
            parts.append(f"default: {_fmt_default(self.default)}")
        return f"({') ('.join(parts)})" if parts else ""


def _fmt_default(value: object) -> str:
    """Render a default value for help (None → 'none', '' → empty-string marker, else str).

    The empty-string check is ``isinstance``-guarded so a non-str default with a permissive
    ``__eq__`` (e.g. a ``pathlib.Path`` or a numpy scalar) can't masquerade as the empty string.
    """
    if value is None:
        return "none"
    if isinstance(value, str) and value == "":
        return "(empty)"
    return str(value)


@dataclass
class Section:
    """A named block of options ("global options:", "options for `run`:").

    ``scope`` (optional) names the subcommand these options belong to — the help-clarity rule
    that subcommand-only options are SCOPED to their subcommand, never dumped into the global
    block. The formatter renders the scope in the heading ("options for `run`:").
    """

    title: str
    options: List[Option] = field(default_factory=list)
    scope: str = ""

    def add(
        self,
        *names: str,
        help: str = "",
        metavar: str = "",
        choices: Optional[Sequence[str]] = None,
        default: object = _UNSET,
    ) -> "Section":
        """Add an option to this section; returns self so adds can chain.

        ``default`` keeps the no-default sentinel when omitted, so NOT passing it leaves the
        Option without a "(default: …)" annotation — passing ``default=None`` would wrongly
        render "(default: none)" for a switch that has no default.
        """
        self.options.append(
            Option(names=list(names), help=help, metavar=metavar, choices=choices, default=default)
        )
        return self


@dataclass
class Subcommand:
    """One subcommand in the subcommands list: ``run    start the server``."""

    name: str
    summary: str = ""


@dataclass
class InstallState:
    """The configured/pending/conflict state of one install/setup surface.

    ``status`` is one of ``ok`` / ``pending`` / ``conflict`` — validated in ``__post_init__`` so a
    typo (``"okk"``) raises rather than silently rendering as pending. ``label`` names the surface
    ("zsh completion", "voice setup", "shell PATH"); ``detail`` is an optional trailing note
    ("installed at ~/.zsh/completions/_tg"). Prefer the :meth:`configured` / :meth:`not_configured`
    / :meth:`conflict` factories over the raw constructor.
    """

    label: str
    status: str  # "ok" | "pending" | "conflict"
    detail: str = ""

    _VALID_STATUSES = ("ok", "pending", "conflict")

    def __post_init__(self) -> None:
        if self.status not in InstallState._VALID_STATUSES:
            raise ValueError(
                f"InstallState.status must be one of {InstallState._VALID_STATUSES}, "
                f"got {self.status!r}"
            )

    @staticmethod
    def configured(label: str, detail: str = "") -> "InstallState":
        return InstallState(label=label, status="ok", detail=detail)

    @staticmethod
    def not_configured(label: str, detail: str = "") -> "InstallState":
        return InstallState(label=label, status="pending", detail=detail)

    @staticmethod
    def conflict(label: str, detail: str = "") -> "InstallState":
        return InstallState(label=label, status="conflict", detail=detail)


def install_state_line(state: InstallState, palette: Optional[Palette] = None) -> str:
    """Render one install-state line: ``✓ zsh completion — installed at …`` (colored by status).

    ✓ green when configured, ○ yellow when pending, ⚠ red when conflicting — the roadmap's
    install-* state vocabulary, one place so every setup/voice/completion surface shows the same
    glyphs.
    """
    p = palette or Palette(False)
    if state.status == "ok":
        glyph = p.ok(GLYPH_OK)
    elif state.status == "conflict":
        glyph = p.err(GLYPH_CONFLICT)
    else:
        glyph = p.warn(GLYPH_PENDING)
    line = f"{glyph} {state.label}"
    if state.detail:
        line += p.dim(f" — {state.detail}")
    return line


# ── topics (topic-help: `<tool> help <topic>`) ────────────────────────────────────────
@dataclass
class Topic:
    """A help topic surfaced as ``<tool> help <topic>``.

    ``name`` — the keyword ("format"); ``summary`` — the one-liner shown when the MAIN help
    advertises available topics; ``body`` — the full text printed by ``<tool> help <name>``.
    Replaces bespoke ``--format-help`` flags with one standard topic-help convention.
    """

    name: str
    summary: str
    body: str


class TopicRegistry:
    """The set of help topics for a tool, with ordered lookup + an advertise list.

    The MAIN ``--help`` advertises the topic NAMES ("see `tg help format`"); ``help <name>``
    renders the full body. Insertion order is preserved so the advertised list is stable.
    """

    def __init__(self) -> None:
        self._topics: Dict[str, Topic] = {}

    def add(self, name: str, summary: str, body: str) -> "TopicRegistry":
        self._topics[name] = Topic(name=name, summary=summary, body=body)
        return self

    def names(self) -> List[str]:
        return list(self._topics)

    def get(self, name: str) -> Optional[Topic]:
        return self._topics.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._topics

    def __len__(self) -> int:
        return len(self._topics)

    def render_topic(self, name: str, *, prog: str = "", palette: Optional[Palette] = None) -> str:
        """Render ``<tool> help <name>`` — the topic's full body with a styled heading.

        Returns the body for a known topic; for an unknown topic returns a short "no such
        topic" block that lists the available topics (so a typo guides the user). Pairs with
        ``agenttools_errors.did_you_mean`` at the CLI layer for a suggestion.
        """
        p = palette or Palette(False)
        topic = self._topics.get(name)
        if topic is None:
            head = p.err(f"no such help topic: {name}")
            if self._topics:
                avail = ", ".join(p.topic(n) for n in self._topics)
                return f"{head}\n{p.dim('available topics:')} {avail}"
            return head
        head = p.heading(f"{prog} help {topic.name}".strip())
        return f"{head}\n\n{topic.body.rstrip()}"


# ── the main help formatter ───────────────────────────────────────────────────────────
@dataclass
class HelpFormatter:
    """Build a CLI's ``--help`` from data; ``.render()`` produces the colorized text.

    Assemble: a ``prog`` + ``tagline`` banner, one or more ``usage`` lines, ``sections`` of
    options (global + per-subcommand scoped), a ``subcommands`` list, ``install_states`` block,
    and an advertised set of help ``topics``. The formatter owns alignment, wrapping, and color
    — so every CLI's help looks the same and the help-clarity rules hold structurally.
    """

    prog: str
    tagline: str = ""
    usages: List[str] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    subcommands: List[Subcommand] = field(default_factory=list)
    install_states: List[InstallState] = field(default_factory=list)
    topics: Optional[TopicRegistry] = None
    epilog: str = ""
    width: int = 0  # 0 → auto-detect terminal width (capped), else a fixed width

    def add_usage(self, usage: str) -> "HelpFormatter":
        self.usages.append(usage)
        return self

    def add_section(self, title: str, scope: str = "") -> Section:
        sec = Section(title=title, scope=scope)
        self.sections.append(sec)
        return sec

    def add_subcommand(self, name: str, summary: str = "") -> "HelpFormatter":
        self.subcommands.append(Subcommand(name=name, summary=summary))
        return self

    def add_install_state(self, state: InstallState) -> "HelpFormatter":
        self.install_states.append(state)
        return self

    def _width(self) -> int:
        if self.width:
            return self.width
        # auto: terminal width capped at 100 (readable line length), floor 60.
        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:
            cols = 80
        return max(60, min(100, cols))

    def render(self, *, color: Optional[bool] = None, stream: object = None) -> str:
        """Render the full ``--help`` text (banner, usage, sections, subcommands, …).

        ``color`` defaults to the capability check on ``stream`` (stdout). Pass ``color=True``/
        ``False`` to force it (tests do). The output never has a trailing blank line.
        """
        if color is None:
            color = should_color(stream)
        p = Palette(color)
        width = self._width()
        out: List[str] = []

        # banner: "prog — tagline"
        if self.tagline:
            out.append(f"{p.title(self.prog)} {p.dim('—')} {self.tagline}")
        else:
            out.append(p.title(self.prog))

        # usage line(s)
        if self.usages:
            out.append("")
            out.append(p.heading("usage:"))
            for u in self.usages:
                out.append(f"  {u}")

        # subcommands list
        if self.subcommands:
            out.append("")
            out.append(p.heading("commands:"))
            out.extend(self._render_pairs(
                [(sc.name, sc.summary) for sc in self.subcommands], p, width, role="subcommand"
            ))

        # option sections (global + per-subcommand scoped)
        for sec in self.sections:
            out.append("")
            # Normalize a caller-supplied trailing ":" off first, so a scope is never dropped
            # for a title like "options:" and we always emit exactly one trailing colon.
            heading = sec.title.rstrip(":")
            if sec.scope and "{scope}" in heading:
                heading = heading.replace("{scope}", sec.scope)
            elif sec.scope:
                heading = f"{heading} for `{sec.scope}`"
            out.append(p.heading(f"{heading}:"))
            out.extend(self._render_options(sec.options, p, width))

        # install/setup state block
        if self.install_states:
            out.append("")
            out.append(p.heading("setup:"))
            for st in self.install_states:
                out.append("  " + install_state_line(st, p))

        # advertised help topics
        if self.topics is not None and len(self.topics):
            out.append("")
            out.append(p.heading("help topics:"))
            out.extend(self._render_pairs(
                [(t, self.topics.get(t).summary) for t in self.topics.names()],
                p, width, role="topic",
            ))
            out.append("")
            example = self.topics.names()[0]
            out.append(p.dim(f"  see `{self.prog} help <topic>`, e.g. `{self.prog} help {example}`"))

        if self.epilog:
            out.append("")
            out.append(self.epilog.rstrip())

        return "\n".join(out)

    # --- internal rendering helpers ---
    def _render_options(self, options: Sequence[Option], p: Palette, width: int) -> List[str]:
        if not options:
            return []
        headers = [o.header() for o in options]
        col = min(max((len(h) for h in headers), default=0), _OPT_COL_CAP)
        lines: List[str] = []
        for opt, header in zip(options, headers):
            painted = p.option(header)
            # The annotation (choices/default) is passed SEPARATELY so wrapping happens on the
            # plain text — color is applied to the annotation tail AFTER wrapping, never inside
            # textwrap's input, so a wrap can never split an ANSI escape (review finding #1).
            lines.extend(self._wrap_pair(header, painted, opt.help, opt.annotations(), p, width, col))
        return lines

    def _render_pairs(
        self, pairs: Sequence[Tuple[str, str]], p: Palette, width: int, *, role: str
    ) -> List[str]:
        col = min(max((len(name) for name, _ in pairs), default=0), _PAIR_COL_CAP)
        lines: List[str] = []
        for name, summary in pairs:
            painted = p.paint(role, name)
            lines.extend(self._wrap_pair(name, painted, summary, "", p, width, col))
        return lines

    @staticmethod
    def _wrap_pair(
        raw_name: str,
        painted_name: str,
        desc: str,
        annotation: str,
        p: Palette,
        width: int,
        col: int,
    ) -> List[str]:
        """Render a two-column "  name    description (annotation)" row, wrapping the description.

        Aligns at ``col`` when the (uncolored) name fits; otherwise puts the description on the
        next line. Wraps the PLAIN text (name length + description + annotation), then re-applies
        the dim color to the annotation's character range AFTER wrapping — so ANSI escapes can
        never be split across a wrap boundary and bleed color into the next line.
        """
        indent = "  "
        # Minimum chars to leave for the description so a row stays within ``width``. Cap the
        # column position so ``pad_to + wrap_width`` never exceeds ``width`` — otherwise a narrow
        # explicit ``width`` (smaller than the column) would overflow the requested line length.
        _MIN_DESC = 12
        pad_to = len(indent) + col + 2  # 2 spaces gutter between columns
        pad_to = min(pad_to, max(len(indent), width - _MIN_DESC))
        first_prefix = f"{indent}{painted_name}"

        # Collapse internal whitespace (tabs / doubled spaces / newlines) in the description to
        # single spaces FIRST, so ``body`` matches what ``textwrap.wrap`` emits (its defaults
        # normalize whitespace). Otherwise ``body.find(chunk)`` below would miss a normalized
        # chunk and the annotation-coloring offsets would drift (review finding: tabbed help).
        desc = " ".join(desc.split())
        body = f"{desc} {annotation}".strip() if annotation else desc
        if not body:
            return [first_prefix]

        # The annotation occupies the TAIL of the plain body; remember where it starts so we can
        # color exactly those characters on each wrapped line.
        ann_start = len(body) - len(annotation) if annotation else len(body)

        wrap_width = max(_MIN_DESC, width - pad_to)
        wrapped = textwrap.wrap(body, width=wrap_width) or [""]
        # Recompute each wrapped chunk's offset into the (whitespace-normalized) body so we know
        # which chars are the annotation. body and the chunks now share one whitespace form, so
        # ``find`` is reliable.
        colored: List[str] = []
        cursor = 0
        for chunk in wrapped:
            idx = body.find(chunk, cursor)
            if idx < 0:
                idx = cursor
            colored.append(_color_tail(chunk, idx, ann_start, p))
            cursor = idx + len(chunk)

        lines: List[str] = []
        if len(raw_name) + len(indent) <= pad_to - 2:
            gap = " " * (pad_to - len(indent) - len(raw_name))
            lines.append(f"{first_prefix}{gap}{colored[0]}")
        else:
            # name too long for the column: description starts on the next line, indented.
            lines.append(first_prefix)
            lines.append(" " * pad_to + colored[0])
        for cont in colored[1:]:
            lines.append(" " * pad_to + cont)
        return lines


# ── convenience: argparse introspection ──────────────────────────────────────────────
def options_from_argparse(parser, *, scope: str = "") -> Section:
    """Build a :class:`Section` from an ``argparse.ArgumentParser`` (best-effort introspection).

    Lets a CLI that already declares its flags in argparse get a formatted help section without
    re-listing them — the same single-source principle the completion generator uses. Reads each
    action's option strings, ``metavar``/``dest``, ``help``, ``default``, and ``choices``. The
    ``-h/--help`` action is skipped (every parser has it; it's noise in the shared help).

    This is intentionally lightweight: it does not try to reproduce argparse's full formatting,
    only to harvest the data the :class:`HelpFormatter` renders. Pass ``scope`` for a
    subcommand's parser so its options render under "options for `<scope>`".
    """
    # argparse is stdlib, but import it lazily (inside the function) so the module's top stays
    # free of it — a consumer that never introspects a parser pays nothing.
    from argparse import SUPPRESS

    sec = Section(title="options", scope=scope)
    for action in getattr(parser, "_actions", []):
        names = list(getattr(action, "option_strings", []) or [])
        if not names:
            continue  # positional — handled in usage, not the options block
        if "-h" in names or "--help" in names:
            continue
        # a value-taking option has nargs != 0; store_true/false have nargs == 0
        takes_value = getattr(action, "nargs", None) != 0
        metavar = ""
        if takes_value:
            metavar = (action.metavar or (action.dest.upper() if action.dest else "")) or ""
            if isinstance(metavar, tuple):
                metavar = metavar[0] if metavar else ""
        opt_kw = dict(
            names=names,
            help=action.help or "",
            metavar=metavar,
            choices=list(action.choices) if action.choices else None,
        )
        default = getattr(action, "default", None)
        # argparse uses SUPPRESS / None defaults for switches; only show a real, value default.
        if takes_value and default is not None and default is not SUPPRESS:
            opt_kw["default"] = default
        sec.options.append(Option(**opt_kw))
    return sec


__all__ = [
    # color
    "Palette",
    "should_color",
    "GLYPH_OK",
    "GLYPH_PENDING",
    "GLYPH_CONFLICT",
    # data types
    "Option",
    "Section",
    "Subcommand",
    "InstallState",
    "Topic",
    "TopicRegistry",
    "HelpFormatter",
    # helpers
    "install_state_line",
    "options_from_argparse",
]
