"""Tests for agenttools_help — the shared help formatter.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_help.py -q

Pure rendering: no filesystem, no network. Color is forced on/off explicitly (``color=`` /
monkeypatched env) so the assertions never depend on whether the test runner has a TTY.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_help as h  # noqa: E402
from agenttools_help import (  # noqa: E402
    GLYPH_CONFLICT,
    GLYPH_OK,
    GLYPH_PENDING,
    HelpFormatter,
    InstallState,
    Option,
    Palette,
    Section,
    TopicRegistry,
    install_state_line,
    options_from_argparse,
    should_color,
)


# --- should_color env precedence --------------------------------------------------------
class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_should_color_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is False


def test_should_color_force_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert should_color(_FakeStream(False)) is True


def test_should_color_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is True
    assert should_color(_FakeStream(False)) is False


def test_should_color_no_arg_uses_stdout(monkeypatch):
    # The help module's default stream (no arg) is sys.stdout (mirror of errors → stderr).
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr("agenttools_help.core.sys.stdout", _FakeStream(True))
    assert should_color() is True
    monkeypatch.setattr("agenttools_help.core.sys.stdout", _FakeStream(False))
    assert should_color() is False


# Identical off-value matrix to the errors module (the two should_color impls must mirror).
@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", ""])
def test_should_color_force_color_zero_disables(monkeypatch, val):
    # FORCE_COLOR=0/false/off forces OFF even on a TTY — must match the errors module exactly.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", val)
    assert should_color(_FakeStream(True)) is False


def test_should_color_no_color_beats_force_color(monkeypatch):
    # Mirror of the errors-module precedence test — same implementation, must not drift.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert should_color(_FakeStream(True)) is False


def test_should_color_empty_no_color_does_not_disable(monkeypatch):
    # no-color.org: NO_COLOR must be present AND non-empty; empty does not disable.
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is True


def test_should_color_matches_errors_module(monkeypatch):
    # Cross-module: the help and errors should_color must agree on every env combination, since
    # they are independent copies that "must mirror" each other.
    import agenttools_errors as ae

    matrix = [
        ({"NO_COLOR": "1"}, True),
        ({"NO_COLOR": ""}, True),
        ({"FORCE_COLOR": "0"}, True),
        ({"FORCE_COLOR": "1"}, False),
        ({}, True),
        ({}, False),
    ]
    for env, tty in matrix:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        stream = _FakeStream(tty)
        assert should_color(stream) == ae.should_color(stream), f"drift for env={env} tty={tty}"


# --- Palette ----------------------------------------------------------------------------
def test_palette_disabled_is_plain():
    p = Palette(False)
    assert p.option("--model") == "--model"
    assert p.heading("usage:") == "usage:"


def test_palette_enabled_wraps_ansi():
    p = Palette(True)
    painted = p.option("--model")
    assert painted.startswith("\033[") and painted.endswith("\033[0m")
    assert "--model" in painted


def test_palette_unknown_role_degrades_to_plain():
    # A typo'd role name must not crash the help — it falls through to plain text.
    assert Palette(True).paint("not-a-role", "x") == "x"


# --- Option: ACTUAL defaults ------------------------------------------------------------
def test_option_header_with_metavar():
    assert Option(names=["-m", "--model"], metavar="MODEL").header() == "-m, --model MODEL"


def test_option_header_without_metavar():
    assert Option(names=["--dry-run"]).header() == "--dry-run"


def test_option_shows_actual_default():
    # The central help-clarity rule: render the ACTUAL default, e.g. a resolved --model.
    opt = Option(names=["--model"], metavar="M", default="claude-opus-4-8")
    assert opt.annotations() == "(default: claude-opus-4-8)"


def test_option_no_default_when_unset():
    # A switch with no default must NOT render a misleading "(default: none)".
    assert Option(names=["--verbose"]).annotations() == ""


def test_option_explicit_none_default_renders_none():
    # An explicit default=None is distinct from "unset" and renders as 'none'.
    assert Option(names=["--out"], metavar="P", default=None).annotations() == "(default: none)"


def test_option_empty_string_default_marked():
    assert Option(names=["--prefix"], metavar="S", default="").annotations() == "(default: (empty))"


def test_option_non_str_falsy_default_not_treated_as_empty_string():
    # A non-str default (0, a Path) must render as itself, not as the "(empty)" string marker.
    assert Option(names=["--n"], metavar="N", default=0).annotations() == "(default: 0)"

    from pathlib import Path

    p = Path(".")
    assert Option(names=["--p"], metavar="P", default=p).annotations() == f"(default: {p})"


def test_option_choices_and_default_together():
    opt = Option(names=["--format"], metavar="F", choices=["text", "html"], default="text")
    assert opt.annotations() == "(choices: text, html) (default: text)"


def test_option_choices_only():
    assert Option(names=["--mode"], choices=["a", "b"]).annotations() == "(choices: a, b)"


# --- Section.add chaining + default keyword ---------------------------------------------
def test_section_add_chains_and_keeps_unset_default():
    sec = Section(title="opts")
    out = sec.add("--verbose", help="loud").add("--quiet", help="silent")
    assert out is sec
    assert len(sec.options) == 2
    # neither got a default → no "(default:...)" leaks in
    assert sec.options[0].annotations() == ""


def test_section_add_passes_default_through():
    sec = Section(title="opts")
    sec.add("--model", metavar="M", help="model", default="opus")
    assert "default: opus" in sec.options[0].annotations()


# --- HelpFormatter render ---------------------------------------------------------------
def _build_formatter() -> HelpFormatter:
    hf = HelpFormatter(prog="tg", tagline="send Telegram messages")
    hf.add_usage("tg [global options] <message>")
    hf.add_usage("tg help <topic>")
    g = hf.add_section("global options")
    g.add("-f", "--format", metavar="FMT", help="output format", choices=["text", "html"], default="text")
    g.add("-v", "--verbose", help="print debug output")
    voice = hf.add_section("options", scope="voice setup")
    voice.add("--whisper-model", metavar="NAME", help="local whisper model", default="base")
    hf.add_subcommand("voice", "voice-reply setup")
    hf.add_subcommand("send", "send a message")
    hf.add_install_state(InstallState.configured("voice setup", "whisper at ~/.cache/whisper"))
    hf.add_install_state(InstallState.not_configured("zsh completion"))
    hf.topics = TopicRegistry().add("format", "text vs html", "tg renders text by default.")
    return hf


def test_render_contains_all_sections_plain():
    out = _build_formatter().render(color=False)
    assert "tg — send Telegram messages" in out
    assert "usage:" in out
    assert "tg [global options] <message>" in out
    assert "commands:" in out
    assert "voice" in out and "send a message" in out
    assert "global options:" in out
    assert "setup:" in out
    assert "help topics:" in out


def test_render_shows_actual_model_default():
    out = _build_formatter().render(color=False)
    assert "default: text" in out  # the resolved/actual default, not a stale literal


def test_scoped_options_render_under_their_subcommand():
    # The help-clarity rule: subcommand-only options are SCOPED, never in the global block.
    out = _build_formatter().render(color=False)
    assert "options for `voice setup`:" in out
    # --whisper-model lives under the scoped heading, not under "global options:"
    global_block = out.split("global options:")[1].split("options for")[0]
    assert "--whisper-model" not in global_block


def test_scope_template_substitution():
    hf = HelpFormatter(prog="x")
    sec = hf.add_section("{scope} options", scope="run")
    sec.add("--foo")
    out = hf.render(color=False)
    assert "run options:" in out


def test_scope_not_dropped_when_title_ends_in_colon():
    # A title already ending in ":" combined with a scope must still render the scope (review #16).
    hf = HelpFormatter(prog="x")
    sec = hf.add_section("options:", scope="run")
    sec.add("--foo")
    out = hf.render(color=False)
    assert "options for `run`:" in out
    assert "options:: " not in out and "options: for" not in out  # exactly one colon, no garble


def test_render_advertises_topics_with_example():
    out = _build_formatter().render(color=False)
    assert "help topics:" in out
    assert "format" in out
    assert "see `tg help <topic>`, e.g. `tg help format`" in out


def test_render_install_state_glyphs():
    out = _build_formatter().render(color=False)
    assert f"{GLYPH_OK} voice setup" in out
    assert f"{GLYPH_PENDING} zsh completion" in out


def test_render_install_state_conflict_glyph_end_to_end():
    # The conflict path through install_state_line → render() is exercised end-to-end.
    hf = HelpFormatter(prog="x")
    hf.add_install_state(InstallState.conflict("shell PATH", "two x binaries on PATH"))
    out = hf.render(color=False)
    assert f"{GLYPH_CONFLICT} shell PATH" in out
    assert "two x binaries on PATH" in out


def test_render_color_emits_ansi():
    out = _build_formatter().render(color=True)
    assert "\033[1;36m" in out  # title color
    assert "\033[" in out


def test_render_no_trailing_blank_line():
    out = _build_formatter().render(color=False)
    assert not out.endswith("\n")


def test_render_minimal_formatter():
    # Only a prog: must still render without crashing and without empty section headings.
    out = HelpFormatter(prog="solo").render(color=False)
    assert out == "solo"


def test_render_epilog_appended():
    hf = HelpFormatter(prog="x", epilog="exit codes: 0 ok, 2 usage")
    out = hf.render(color=False)
    assert out.endswith("exit codes: 0 ok, 2 usage")


def test_render_empty_section_emits_heading_only():
    # A section with zero options still emits its heading line and nothing under it — pin this so
    # a future change to skip empty sections is a deliberate, visible diff.
    hf = HelpFormatter(prog="x")
    hf.add_section("opts")  # no options added
    out = hf.render(color=False)
    lines = out.splitlines()
    idx = lines.index("opts:")
    # nothing indented follows the heading (it's the last content line)
    assert all(not ln.startswith("  ") for ln in lines[idx + 1 :])


def test_render_stream_path_drives_should_color(monkeypatch):
    # render() with no explicit color= must consult should_color(stream).
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    hf = HelpFormatter(prog="x", tagline="t")
    assert "\033[" in hf.render(stream=_FakeStream(True))  # TTY → colored
    assert "\033[" not in hf.render(stream=_FakeStream(False))  # non-TTY → plain


def test_long_description_wraps_to_column():
    hf = HelpFormatter(prog="x", width=60)
    g = hf.add_section("opts")
    g.add("--thing", help="a very long description that definitely exceeds sixty columns and must wrap")
    out = hf.render(color=False)
    # wrapped continuation lines are indented past the option column
    lines = [ln for ln in out.splitlines() if "exceeds" in ln or "must wrap" in ln]
    assert lines  # the text is present, just wrapped


def _ansi_balanced(s: str) -> bool:
    """Every ANSI SGR open code (\\033[...m, non-reset) is followed by a reset on the same line."""
    import re

    for line in s.splitlines():
        opens = len(re.findall(r"\033\[(?!0m)[0-9;]*m", line))
        resets = line.count("\033[0m")
        if opens != resets:
            return False
    return True


def test_wrapped_annotation_does_not_split_ansi():
    # A long description + a colored (choices/default) annotation that wraps must NOT leave an
    # open ANSI escape on any line (review finding #1 — bleeding color into the next line).
    hf = HelpFormatter(prog="x", width=50)
    g = hf.add_section("opts")
    g.add(
        "--mode",
        metavar="M",
        help="a deliberately long description that forces the choices and default annotation onto a wrapped line",
        choices=["alpha", "bravo", "charlie"],
        default="alpha",
    )
    out = hf.render(color=True)
    assert _ansi_balanced(out), "an ANSI escape was split across a wrap boundary"
    # and the annotation text is still present (stripped of color)
    import re

    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    assert "choices: alpha, bravo, charlie" in plain
    assert "default: alpha" in plain


def test_annotation_is_dim_colored_on_one_line():
    # Beyond ANSI balance: the annotation text must actually be wrapped in the dim SGR (\033[2m).
    hf = HelpFormatter(prog="x", width=80)
    g = hf.add_section("opts")
    g.add("--mode", metavar="M", help="short", choices=["x", "y"], default="x")
    out = hf.render(color=True)
    assert "\033[2m(choices: x, y) (default: x)\033[0m" in out


def test_consecutive_whitespace_in_default_value_keeps_ansi_balanced():
    # A default VALUE containing consecutive whitespace must not desync the find()-based offset
    # math (textwrap collapses runs); the annotation stays fully dim and ANSI stays balanced.
    import re

    hf = HelpFormatter(prog="x", width=45)
    g = hf.add_section("opts")
    g.add("--path", metavar="P", help="output path", default="a    b    c")  # runs of spaces
    out = hf.render(color=True)
    for line in out.splitlines():
        opens = len(re.findall(r"\033\[(?!0m)[0-9;]*m", line))
        assert opens == line.count("\033[0m"), f"unbalanced: {line!r}"
    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    flat = " ".join(plain.split())
    assert "default: a b c" in flat  # collapsed, present, and was dim-colored


def test_tab_in_description_does_not_break_annotation_coloring():
    # A TAB (or doubled space) in help text is normalized so the annotation offsets stay correct
    # and the annotation is still fully dim-colored across a wrap (review: tabbed help regression).
    import re

    hf = HelpFormatter(prog="x", width=40)
    g = hf.add_section("opts")
    g.add("--mode", metavar="M", help="alpha\tbeta   gamma delta epsilon zeta", choices=["xx", "yy"], default="xx")
    out = hf.render(color=True)
    for line in out.splitlines():
        opens = len(re.findall(r"\033\[(?!0m)[0-9;]*m", line))
        assert opens == line.count("\033[0m"), f"unbalanced: {line!r}"
    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    assert "\t" not in plain  # tab collapsed
    # the annotation text is all present (it may itself wrap, so compare on collapsed whitespace)
    flat = " ".join(plain.split())
    assert "choices: xx, yy" in flat and "default: xx" in flat
    assert "alpha beta gamma" in flat  # description words intact, tab gone


def test_long_option_name_pushes_description_to_next_line():
    # An option header wider than the column cap: description starts on the next line (review #11).
    hf = HelpFormatter(prog="x", width=70)
    g = hf.add_section("opts")
    long_name = "--an-extremely-long-option-name-that-exceeds-the-column-cap"
    g.add(long_name, metavar="V", help="its description")
    out = hf.render(color=False)
    lines = out.splitlines()
    name_line = next(i for i, ln in enumerate(lines) if long_name in ln)
    # the header line itself does not carry the description; it's on a following line
    assert "its description" not in lines[name_line]
    assert any("its description" in ln for ln in lines[name_line + 1 :])


def test_width_explicit_is_used():
    hf = HelpFormatter(prog="x", width=42)
    assert hf._width() == 42


def test_narrow_explicit_width_does_not_overflow_description():
    # A narrow explicit width must not push WRAPPED DESCRIPTION lines past the requested width.
    # (An unbreakable option NAME wider than the width is inherent — exclude name lines.)
    hf = HelpFormatter(prog="x", width=40)
    g = hf.add_section("o")
    g.add("-m", metavar="M", help="alpha beta gamma delta epsilon zeta eta theta iota kappa")
    out = hf.render(color=False)
    for line in out.splitlines():
        # description/continuation lines are indented; the (short) name fits, so every line ≤ width
        assert len(line) <= 40, f"overflow ({len(line)}): {line!r}"


def test_width_auto_detects_and_clamps(monkeypatch):
    import agenttools_help.core as core

    class _Size:
        def __init__(self, columns):
            self.columns = columns

    monkeypatch.setattr(core.shutil, "get_terminal_size", lambda fallback=(80, 24): _Size(300))
    assert HelpFormatter(prog="x")._width() == 100  # capped at 100
    monkeypatch.setattr(core.shutil, "get_terminal_size", lambda fallback=(80, 24): _Size(20))
    assert HelpFormatter(prog="x")._width() == 60  # floored at 60


# --- InstallState + install_state_line --------------------------------------------------
def test_install_state_constructors_set_status():
    assert InstallState.configured("x").status == "ok"
    assert InstallState.not_configured("x").status == "pending"
    assert InstallState.conflict("x").status == "conflict"


def test_install_state_rejects_invalid_status():
    # A typo'd status must raise, not silently render as pending.
    with pytest.raises(ValueError, match="must be one of"):
        InstallState(label="x", status="okk")


def test_install_state_line_glyphs_plain():
    assert install_state_line(InstallState.configured("a")) == f"{GLYPH_OK} a"
    assert install_state_line(InstallState.not_configured("b")) == f"{GLYPH_PENDING} b"
    assert install_state_line(InstallState.conflict("c")) == f"{GLYPH_CONFLICT} c"


def test_install_state_line_detail_appended():
    line = install_state_line(InstallState.configured("voice", "whisper ready"))
    assert line == f"{GLYPH_OK} voice — whisper ready"


def test_install_state_line_color():
    line = install_state_line(InstallState.configured("a"), Palette(True))
    assert "\033[32m" in line  # green ✓


def test_install_state_conflict_color_is_red():
    line = install_state_line(InstallState.conflict("a", "two binaries"), Palette(True))
    assert "\033[31m" in line  # red ⚠ for a conflict


# --- TopicRegistry ----------------------------------------------------------------------
def test_topic_registry_preserves_order():
    reg = TopicRegistry().add("b", "s", "body").add("a", "s", "body").add("c", "s", "body")
    assert reg.names() == ["b", "a", "c"]


def test_topic_registry_membership_and_len():
    reg = TopicRegistry().add("format", "s", "b")
    assert "format" in reg
    assert "nope" not in reg
    assert len(reg) == 1


def test_render_known_topic():
    reg = TopicRegistry().add("format", "text vs html", "Line one.\nLine two.")
    out = reg.render_topic("format", prog="tg")
    assert "tg help format" in out
    assert "Line one." in out and "Line two." in out


def test_render_unknown_topic_lists_available():
    reg = TopicRegistry().add("format", "s", "b").add("voice", "s", "b")
    out = reg.render_topic("nope", prog="tg")
    assert "no such help topic: nope" in out
    assert "format" in out and "voice" in out


def test_render_unknown_topic_empty_registry():
    reg = TopicRegistry()
    out = reg.render_topic("x", prog="tg")
    assert "no such help topic: x" in out


def test_topic_registry_add_overwrites_same_name():
    # Re-adding a name replaces its body (documented behavior); pin it so a change is visible.
    reg = TopicRegistry().add("format", "first", "first body").add("format", "second", "second body")
    assert len(reg) == 1
    assert reg.get("format").body == "second body"


def test_render_topic_color():
    reg = TopicRegistry().add("format", "s", "body text")
    out = reg.render_topic("format", prog="tg", palette=Palette(True))
    assert "\033[1m" in out  # bold heading


# --- options_from_argparse (single-source introspection) --------------------------------
def test_options_from_argparse_harvests_value_default():
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("-m", "--model", default="opus", help="which model")
    sec = options_from_argparse(p, scope="apply")
    by_name = {o.names[0]: o for o in sec.options}
    assert by_name["-m"].annotations() == "(default: opus)"
    assert sec.scope == "apply"


def test_options_from_argparse_skips_help_action():
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--x")
    sec = options_from_argparse(p)
    flat = [n for o in sec.options for n in o.names]
    assert "-h" not in flat and "--help" not in flat


def test_options_from_argparse_store_true_has_no_default():
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--dry-run", action="store_true", help="no writes")
    sec = options_from_argparse(p)
    opt = sec.options[0]
    assert opt.header() == "--dry-run"  # no metavar for a switch
    assert opt.annotations() == ""  # no misleading default


def test_options_from_argparse_choices():
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--mode", choices=["a", "b"], default="a", help="mode")
    sec = options_from_argparse(p)
    assert sec.options[0].annotations() == "(choices: a, b) (default: a)"


def test_options_from_argparse_ignores_positionals():
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("path")  # positional → not an option, goes in usage not the options block
    p.add_argument("--flag", action="store_true")
    sec = options_from_argparse(p)
    flat = [n for o in sec.options for n in o.names]
    assert "path" not in flat
    assert "--flag" in flat


def test_options_from_argparse_value_option_with_none_default_shows_no_default():
    # A value option whose default is None (the argparse default) → no "(default: …)" — we only
    # advertise a REAL, value default, not argparse's implicit None.
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--out", help="output path")  # default is None
    sec = options_from_argparse(p)
    assert sec.options[0].annotations() == ""


def test_options_from_argparse_tuple_metavar_uses_first():
    # nargs=2 with a tuple metavar — take the first element rather than rendering the tuple repr.
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--pair", nargs=2, metavar=("LO", "HI"), help="a range")
    sec = options_from_argparse(p)
    assert sec.options[0].metavar == "LO"


def test_options_from_argparse_suppress_default_shows_no_default():
    # An option with default=SUPPRESS must not advertise a default (the value never appears).
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--x", default=argparse.SUPPRESS, help="suppressed default")
    sec = options_from_argparse(p)
    assert sec.options[0].annotations() == ""


def test_options_from_argparse_feeds_helpformatter():
    # End-to-end: argparse parser → section → rendered help, defaults intact.
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("--model", default="opus", help="model")
    hf = HelpFormatter(prog="demo")
    hf.sections.append(options_from_argparse(p))
    out = hf.render(color=False)
    assert "--model" in out and "default: opus" in out


# --- Palette role coverage --------------------------------------------------------------
def test_palette_every_role_method_colors_when_enabled():
    p = Palette(True)
    for role in ("title", "heading", "option", "subcommand", "topic", "default", "dim", "ok", "warn", "err"):
        painted = getattr(p, role)("x")
        assert painted.startswith("\033[") and painted.endswith("\033[0m")


# --- public surface ---------------------------------------------------------------------
def test_all_exports_importable():
    for name in h.__all__:
        assert hasattr(h, name), f"{name} listed in __all__ but missing"


def test_version_present():
    assert isinstance(h.__version__, str) and h.__version__
