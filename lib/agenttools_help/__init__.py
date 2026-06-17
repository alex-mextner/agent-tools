"""agenttools_help — one shared help formatter for every agent-tools CLI.

``tg``'s ``--help`` was NOT colorized like ``review`` / ``rig`` / ``draw`` — inconsistent — and
every tool re-implemented section layout, the usage line, and the subcommands list slightly
differently. This is the ONE shared help layer the roadmap calls for: colors/styling, section
layout, usage line(s), subcommands list, ACTUAL-defaults rendering, subcommand-scoped options,
topic-help (``<tool> help <topic>``), and install-* state (✓ configured / ○ pending / ⚠
conflict). Build help as data here and a fix lands across every CLI at once.

Quick start
-----------
    from agenttools_help import HelpFormatter, InstallState, TopicRegistry

    topics = TopicRegistry().add(
        "format", "text vs html output", "tg renders plain text by default …"
    )

    hf = HelpFormatter(prog="tg", tagline="send Telegram messages from any agent")
    hf.add_usage("tg [global options] <message>")
    hf.add_usage("tg help <topic>")

    g = hf.add_section("global options")
    g.add("-f", "--format", metavar="FMT", help="output format", choices=["text", "html"],
          default="text")          # ACTUAL default shown

    run = hf.add_section("options", scope="voice setup")   # scoped to a subcommand
    run.add("--whisper-model", metavar="NAME", help="local whisper model", default="base")

    hf.add_subcommand("voice", "voice-reply setup + transcription")
    hf.add_install_state(InstallState.configured("voice setup", "whisper at ~/.cache/whisper"))
    hf.topics = topics

    print(hf.render())                       # <tool> --help
    print(topics.render_topic("format", prog="tg"))   # <tool> help format

Color follows ``NO_COLOR`` / ``FORCE_COLOR`` / TTY (same precedence as
``agenttools_errors``). The full reference lives in ``lib/agenttools_help/README.md``.
"""

from __future__ import annotations

from .core import (
    GLYPH_CONFLICT,
    GLYPH_OK,
    GLYPH_PENDING,
    HelpFormatter,
    InstallState,
    Option,
    Palette,
    Section,
    Subcommand,
    Topic,
    TopicRegistry,
    install_state_line,
    options_from_argparse,
    should_color,
)

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

__version__ = "0.1.0"
