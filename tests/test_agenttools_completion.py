"""Tests for agenttools_completion — the universal zsh tab-completion generator + installer.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_completion.py -q
    # or, if agenttools-completion is installed:  python -m pytest tests/ -q

The generator tests assert structural facts about the emitted script (it starts with
``#compdef``, names every subcommand / long option / choice value, never leaks the literal
``==SUPPRESS==``). The non-negotiable test is ``test_generated_script_parses_in_real_zsh``:
it shells out to the real ``zsh`` binary and runs ``zsh -n`` on the generated file, proving
the script actually parses. The installer tests use a tmp comp_dir + tmp zshrc so they never
touch the real ``~/.zshrc``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_completion as atc  # noqa: E402
from agenttools_completion import (  # noqa: E402
    InstallResult,
    UninstallResult,
    generate_zsh,
    install,
    status,
    uninstall,
)
from agenttools_completion.demo_cli import build_demo_parser  # noqa: E402


# --------------------------------------------------------------------------- fixtures


def _sample_parser() -> argparse.ArgumentParser:
    """A parser exercising subcommands, options, choices, a nested subparser, SUPPRESS,
    and a help string laced with zsh-hostile characters (a colon and a single-quote)."""
    p = argparse.ArgumentParser(prog="sample", description="sample tool")
    p.add_argument("--verbose", "-v", action="store_true", help="be loud")
    p.add_argument(
        "--mode",
        choices=["fast", "slow"],
        help="speed: pick fast or slow (it's your call)",
    )
    # A value-taking option with NO choices — must complete to a value (files), not a flag.
    p.add_argument("--out", help="output path")
    p.add_argument("--secret", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command")

    build = sub.add_parser("build", help="build the thing")
    build.add_argument("--target", choices=["debug", "release"], help="build target")
    build.add_argument("path", nargs="?", help="source path")

    stats = sub.add_parser("stats", help="stats: show numbers (don't panic)")
    stats_sub = stats.add_subparsers(dest="stats_command")
    show = stats_sub.add_parser("show", help="show a single stat")
    show.add_argument("--format", choices=["json", "table"], help="output format")
    stats_sub.add_parser("clear", help="clear all stats")
    # A subparser with a description but no help= — its label must fall back to .description.
    stats_sub.add_parser("dump", description="dump raw stats")

    return p


# --------------------------------------------------------------------------- generator


def test_generate_starts_with_compdef_and_defines_function() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    assert out.startswith("#compdef sample")
    assert "_sample() {" in out
    # The dispatcher must end by invoking the top-level function (zsh #compdef convention).
    assert out.rstrip().endswith('_sample "$@"')


def test_generate_contains_every_subcommand() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    for name in ("build", "stats", "show", "clear"):
        assert name in out, f"subcommand {name!r} missing from generated script"


def test_generate_contains_long_options() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    for opt in ("--verbose", "--mode", "--target", "--format"):
        assert opt in out, f"option {opt!r} missing from generated script"


def test_generate_contains_short_options() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    assert "-v" in out


def test_generate_contains_choice_values() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    for val in ("fast", "slow", "debug", "release", "json", "table"):
        assert val in out, f"choice value {val!r} missing from generated script"


def test_generate_never_leaks_suppress_literal() -> None:
    out = generate_zsh(_sample_parser(), "sample")
    assert "SUPPRESS" not in out, "argparse.SUPPRESS help leaked into the script"


def test_generate_escapes_colon_and_quote_in_help() -> None:
    """A help string with a ``:`` and a ``'`` must not produce a spec where the colon
    is bare (which would split the _arguments spec) or the quote is unescaped."""
    out = generate_zsh(_sample_parser(), "sample")
    # The hostile help text was "speed: pick fast or slow (it's your call)". The raw colon
    # after "speed" must NOT survive verbatim inside a spec (it would split the spec); it
    # must appear backslash-escaped instead.
    assert "speed:" not in out, "an unescaped colon survived in a description"
    assert "speed\\:" in out, "the colon was not backslash-escaped"
    # The apostrophe MUST be escaped via the exact zsh '\'' idiom — not silently dropped.
    assert "it'\\''s" in out, "the single-quote was not escaped with the '\\'' idiom"


def test_generate_handles_parser_with_no_subcommands() -> None:
    p = argparse.ArgumentParser(prog="flat", description="no subcommands")
    p.add_argument("--name", help="a name")
    out = generate_zsh(p, "flat")
    assert out.startswith("#compdef flat")
    assert "--name" in out


def test_value_taking_option_without_choices_completes_files() -> None:
    """``--out PATH`` (a value-taking option with no enumerated choices) must request a
    value completion (``_files``), not be emitted as a bare no-arg flag."""
    out = generate_zsh(_sample_parser(), "sample")
    assert "--out" in out
    assert "_files" in out, "a value-taking option got no value completion"


def test_store_true_flag_is_not_value_taking() -> None:
    """A ``store_true`` flag (``--verbose``) must NOT get a ``:value:`` action."""
    p = argparse.ArgumentParser(prog="t")
    p.add_argument("--flag", action="store_true", help="just a flag")
    out = generate_zsh(p, "t")
    # The only spec for --flag is the bracketed description; no :_files / value action.
    assert "'--flag[just a flag]'" in out


def test_subparser_description_used_when_no_help() -> None:
    """A subparser declared with ``description=`` but no ``help=`` still gets a label."""
    out = generate_zsh(_sample_parser(), "sample")
    # 'dump' was added with description="dump raw stats" and no help=.
    assert "dump raw stats" in out


def test_subparser_help_colon_is_escaped() -> None:
    """A subcommand help containing ':' must be escaped so _describe doesn't mis-split it."""
    out = generate_zsh(_sample_parser(), "sample")
    # 'stats' help was "stats: show numbers (don't panic)" — the colon must be escaped.
    assert "'stats:stats\\:" in out or "stats\\:" in out


def test_nested_dispatch_uses_line_not_words() -> None:
    """Nested subcommand dispatch must switch on $line[1] (the parsed positional), so a
    global flag before the subcommand doesn't misroute."""
    out = generate_zsh(_sample_parser(), "sample")
    assert "case $line[1] in" in out
    assert "case $words[1] in" not in out


def test_top_level_positional_choices_emit_values() -> None:
    """A top-level positional with choices (no subcommands) emits a _values group."""
    p = argparse.ArgumentParser(prog="pick")
    p.add_argument("color", choices=["red", "green", "blue"], help="a color")
    out = generate_zsh(p, "pick")
    assert "_values" in out
    for c in ("red", "green", "blue"):
        assert c in out


def test_positional_choice_with_metacharacters_is_safe(tmp_path: Path) -> None:
    """A positional ``choices`` value containing shell metacharacters (``;`` ``&`` ``|`` ``$``
    backtick) must be emitted so the generated ``_values`` line treats it as a single literal
    argument — never breaking out into a second command. An unquoted ``bad; echo PWNED`` would
    emit ``_values 'value' bad;\\ echo\\ PWNED``, where the ``;`` ends the command and zsh then
    parses ``echo PWNED`` as a separate statement; single-quoting each choice prevents that."""
    metachar_choices = ["bad; echo PWNED", "a&b|c", "$(whoami)", "`id`"]
    p = argparse.ArgumentParser(prog="meta")
    p.add_argument("token", choices=["safe", *metachar_choices])
    out = generate_zsh(p, "meta")

    # Positively verify structure: on the _values line every choice is one single-quoted word,
    # so its metacharacters are inert text rather than parsed shell syntax.
    values_line = next(ln for ln in out.splitlines() if ln.lstrip().startswith("_values"))
    for choice in ["safe", *metachar_choices]:
        assert f"'{choice}'" in values_line, f"choice {choice!r} not single-quoted on _values line"

    # Strongest proof: the generated script must still parse in real zsh.
    if shutil.which("zsh") is not None:
        comp_file = tmp_path / "_meta"
        comp_file.write_text(out)
        proc = subprocess.run(
            ["zsh", "-n", str(comp_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"zsh -n rejected a script with a metacharacter-laced positional choice "
            f"(exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def test_snippet_runs_compinit_after_fpath_change(tmp_path: Path) -> None:
    """The managed zshrc block must (re-)run ``compinit`` AFTER prepending the comp_dir to
    ``fpath`` — even when the user's own ``.zshrc`` already ran ``compinit`` earlier.

    ``compinit`` scans ``$fpath`` at the moment it runs. A guard like
    ``if (( ! $+functions[compinit] ))`` skips the re-run whenever the function is already
    defined (the user ran it before this block), so its earlier scan of the OLD fpath misses
    our directory and the freshly-written ``_<prog>`` file is never registered. The block must
    therefore run compinit unconditionally, after the fpath change."""
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=comp_dir, zshrc=zshrc)
    text = zshrc.read_text()

    # Line-oriented (comments mention compinit too): find the fpath line and the actual
    # compinit COMMAND line, and assert the command runs after the fpath change.
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    fpath_pos = next(i for i, ln in enumerate(code_lines) if ln.startswith(f'fpath=("{comp_dir}"'))
    compinit_pos = next(i for i, ln in enumerate(code_lines) if "compinit" in ln and "fpath" not in ln)
    assert compinit_pos > fpath_pos, "compinit must run after the fpath addition"

    # It must NOT be gated such that an already-defined compinit prevents the re-run.
    assert "$+functions[compinit]" not in text, (
        "compinit is skipped when the user already defined it — new completions won't load"
    )


def test_snippet_compinit_loads_new_completion_after_prior_compinit(tmp_path: Path) -> None:
    """End-to-end zsh proof: simulate a user .zshrc that runs ``compinit`` BEFORE our managed
    block, then source the resulting file and assert the freshly-added completion (our
    ``_demo``) is actually registered. The pre-existing compinit must NOT stop our block from
    re-scanning fpath."""
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")

    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    zcompdump_pre = tmp_path / ".zcompdump_pre"
    zcompdump_block = tmp_path / ".zcompdump_block"

    # A user .zshrc that runs compinit FIRST (defining the function + scanning the old fpath),
    # then our managed block.
    user_pre = (
        "autoload -Uz compinit\n"
        f"compinit -u -d {zcompdump_pre}\n"
    )
    zshrc.write_text(user_pre)
    install("demo", generate_zsh(build_demo_parser(), "demo"), comp_dir=comp_dir, zshrc=zshrc)

    # Source the full .zshrc in a non-interactive zsh and check that _demo became a known
    # completion command PURELY as a result of the managed block re-running compinit after the
    # fpath change — no fallback autoload here, so the prior compinit + our block are the only
    # things that could have registered it. With the old guarded block (compinit skipped when
    # already defined) the prior compinit scanned an fpath without comp_dir, so _comps[demo]
    # would be empty and this fails.
    script = (
        "set -e\n"
        f"export ZSH_COMPDUMP={zcompdump_block}\n"
        f"source {zshrc}\n"
        # compinit records each #compdef'd command in the _comps associative array. If our
        # block re-scanned fpath, _comps[demo] points at the _demo function.
        '[[ -n "${_comps[demo]}" ]] || { echo "NOT REGISTERED: _comps[demo] empty"; exit 1; }\n'
        "echo OK\n"
    )
    proc = subprocess.run(
        ["zsh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"the managed block did not make _demo loadable after a prior compinit "
        f"(exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_suppressed_subparser_help_does_not_leak() -> None:
    """``add_parser('x', help=argparse.SUPPRESS)`` must not leak the SUPPRESS literal."""
    p = argparse.ArgumentParser(prog="s")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("hidden", help=argparse.SUPPRESS)
    sub.add_parser("shown", help="visible")
    out = generate_zsh(p, "s")
    assert "SUPPRESS" not in out
    assert "hidden" in out  # the name still completes; only the description is empty


def test_version_exported() -> None:
    assert atc.__version__ == "0.1.0"


# ----------------------------------------------------------------------- demo CLI proof


def test_demo_parser_generates() -> None:
    out = generate_zsh(build_demo_parser(), "demo")
    assert out.startswith("#compdef demo")
    assert "greet" in out
    assert "config" in out  # the nested subparser parent


# ------------------------------------------------------------------------- installer


def test_install_writes_file_and_snippet(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    script = generate_zsh(_sample_parser(), "sample")

    res = install("sample", script, comp_dir=comp_dir, zshrc=zshrc)
    assert isinstance(res, InstallResult)

    comp_file = comp_dir / "_sample"
    assert comp_file.exists()
    assert comp_file.read_text().startswith("#compdef sample")
    # 0644 file mode.
    assert (comp_file.stat().st_mode & 0o777) == 0o644

    text = zshrc.read_text()
    assert "# >>> agent-tools completions >>>" in text
    assert "# <<< agent-tools completions <<<" in text
    assert str(comp_dir) in text


def test_install_is_idempotent(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    script = generate_zsh(_sample_parser(), "sample")

    install("sample", script, comp_dir=comp_dir, zshrc=zshrc)
    install("sample", script, comp_dir=comp_dir, zshrc=zshrc)

    text = zshrc.read_text()
    assert text.count("# >>> agent-tools completions >>>") == 1
    assert text.count("# <<< agent-tools completions <<<") == 1
    # Still exactly one completion file.
    assert sorted(p.name for p in comp_dir.glob("_*")) == ["_sample"]


def test_install_preserves_existing_zshrc_content(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export FOO=bar\nalias ll='ls -la'\n")
    script = generate_zsh(_sample_parser(), "sample")

    install("sample", script, comp_dir=comp_dir, zshrc=zshrc)
    text = zshrc.read_text()
    assert "export FOO=bar" in text
    assert "alias ll='ls -la'" in text


def test_status_reflects_install_state(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    script = generate_zsh(_sample_parser(), "sample")

    before = status("sample", comp_dir=comp_dir, zshrc=zshrc)
    assert before.installed is False

    install("sample", script, comp_dir=comp_dir, zshrc=zshrc)
    after = status("sample", comp_dir=comp_dir, zshrc=zshrc)
    assert after.installed is True
    assert after.file_present is True
    assert after.fpath_configured is True
    assert "✓" in after.human


def test_uninstall_removes_file_and_snippet(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    script = generate_zsh(_sample_parser(), "sample")

    install("sample", script, comp_dir=comp_dir, zshrc=zshrc)
    uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)

    assert not (comp_dir / "_sample").exists()
    # No remaining managed _* files -> snippet block removed.
    text = zshrc.read_text() if zshrc.exists() else ""
    assert "# >>> agent-tools completions >>>" not in text

    after = status("sample", comp_dir=comp_dir, zshrc=zshrc)
    assert after.installed is False


def test_uninstall_keeps_snippet_when_other_completions_remain(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=comp_dir, zshrc=zshrc)
    install("demo", generate_zsh(build_demo_parser(), "demo"), comp_dir=comp_dir, zshrc=zshrc)

    uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)

    assert not (comp_dir / "_sample").exists()
    assert (comp_dir / "_demo").exists()
    # _demo still managed here, so the snippet must remain.
    assert "# >>> agent-tools completions >>>" in zshrc.read_text()


def test_uninstall_is_idempotent(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=comp_dir, zshrc=zshrc)
    uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)
    # Second uninstall must not raise.
    uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)


def test_preserve_other_zshrc_after_uninstall(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export KEEP=1\n")
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=comp_dir, zshrc=zshrc)
    uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)
    assert "export KEEP=1" in zshrc.read_text()


def test_uninstall_returns_uninstall_result(tmp_path: Path) -> None:
    comp_dir = tmp_path / "completions"
    zshrc = tmp_path / ".zshrc"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=comp_dir, zshrc=zshrc)
    res = uninstall("sample", comp_dir=comp_dir, zshrc=zshrc)
    assert isinstance(res, UninstallResult)
    assert res.file_removed is True
    assert res.snippet_removed is True


def test_install_rewrites_snippet_for_different_comp_dir(tmp_path: Path) -> None:
    """Re-installing with a DIFFERENT comp_dir rewrites the block in place (no duplicate),
    and the block now points at the new dir."""
    zshrc = tmp_path / ".zshrc"
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=dir_a, zshrc=zshrc)
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=dir_b, zshrc=zshrc)
    text = zshrc.read_text()
    assert text.count("# >>> agent-tools completions >>>") == 1
    assert str(dir_b) in text
    assert str(dir_a) not in text


def test_status_is_comp_dir_aware(tmp_path: Path) -> None:
    """A block pointing at dir A must not make status(dir B) report fpath_configured."""
    zshrc = tmp_path / ".zshrc"
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=dir_a, zshrc=zshrc)
    st_b = status("sample", comp_dir=dir_b, zshrc=zshrc)
    assert st_b.fpath_configured is False


def test_uninstall_does_not_strip_unrelated_block(tmp_path: Path) -> None:
    """uninstall for dir B (empty) must not strip a managed block that targets dir A."""
    zshrc = tmp_path / ".zshrc"
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    install("sample", generate_zsh(_sample_parser(), "sample"), comp_dir=dir_a, zshrc=zshrc)
    uninstall("ghost", comp_dir=dir_b, zshrc=zshrc)  # nothing in dir_b
    # The block for dir_a must survive.
    assert "# >>> agent-tools completions >>>" in zshrc.read_text()
    assert str(dir_a) in zshrc.read_text()


# ---------------------------------------------------------- the non-negotiable: real zsh


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_generated_script_parses_in_real_zsh(tmp_path: Path) -> None:
    """Write the generated _demo into a tmp file and run ``zsh -n`` (syntax check, no
    exec). Exit 0 proves the emitted script actually PARSES in real zsh — the load-bearing
    guarantee. We use the demo parser (always present) so this never depends on rig-cli."""
    comp_file = tmp_path / "_demo"
    comp_file.write_text(generate_zsh(build_demo_parser(), "demo"))

    proc = subprocess.run(
        ["zsh", "-n", str(comp_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"zsh -n rejected the generated script (exit {proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_generated_script_autoloads_in_real_zsh(tmp_path: Path) -> None:
    """Stronger check: drop _demo into an fpath dir, run compinit, autoload the function,
    and assert it loads as a function. Proves the #compdef tag + body are coherent enough
    for zsh's completion machinery to accept, not just to parse."""
    fpath_dir = tmp_path / "comp"
    fpath_dir.mkdir()
    (fpath_dir / "_demo").write_text(generate_zsh(build_demo_parser(), "demo"))
    zcompdump = tmp_path / ".zcompdump"

    script = (
        f"set -e\n"
        f"fpath=({fpath_dir} $fpath)\n"
        f"autoload -Uz compinit\n"
        f"compinit -u -d {zcompdump}\n"
        f"autoload -Uz _demo\n"
        f"(( $+functions[_demo] )) || {{ echo 'NOT LOADABLE'; exit 1; }}\n"
        f"echo OK\n"
    )
    proc = subprocess.run(
        ["zsh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"zsh failed to autoload _demo (exit {proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# -------------------------------------------------- best-effort real-tool proof: rig-cli


def test_rig_cli_generates_if_importable() -> None:
    """Best-effort: if rig-cli is importable, generate a real _rig and sanity-check it.
    Gated so the unit suite never HARD-depends on rig-cli being present on this machine."""
    rig_root = Path(os.environ.get("RIG_CLI_ROOT", "/Users/ultra/xp/rig-cli"))
    if not rig_root.exists():
        pytest.skip("rig-cli checkout not present (set RIG_CLI_ROOT to override)")
    if str(rig_root) not in sys.path:
        sys.path.insert(0, str(rig_root))
    try:
        from riglib.cli import build_parser  # noqa: WPS433
    except ImportError as exc:
        pytest.skip(f"rig-cli not importable: {exc}")

    out = generate_zsh(build_parser(), "rig")
    assert out.startswith("#compdef rig")
    assert "_rig()" in out
    # rig has real subcommands; at least one well-known one should appear.
    assert "apply" in out or "status" in out
