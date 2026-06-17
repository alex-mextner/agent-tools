"""Tests for agenttools_errors — the shared error/exit-code layer.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_errors.py -q

No filesystem or network is touched: the module is pure (dataclasses + string rendering +
``shutil.which`` for the one dependency probe, which is stubbed). The exit-code constants are
asserted as a PUBLIC CONTRACT — a value change here is a deliberate breaking change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import copy  # noqa: E402
import pickle  # noqa: E402

import agenttools_errors as e  # noqa: E402
from agenttools_errors import (  # noqa: E402
    AgentToolError,
    MissingDepError,
    PermissionDeniedError,
    RemovedSlot,
    RemovedSlotRegistry,
    UnknownItemError,
    UsageError,
    did_you_mean,
    guard,
    missing_dep_error,
    missing_target_error,
    not_a_repo_error,
    render,
    require_tool,
    should_color,
    unknown_item_error,
)


# --- exit-code contract -----------------------------------------------------------------
def test_exit_codes_are_the_documented_contract():
    # These values are a PUBLIC CONTRACT; pin them so a renumber is a deliberate, visible diff.
    assert e.EXIT_OK == 0
    assert e.EXIT_INTERNAL == 1
    assert e.EXIT_USAGE == 2
    assert e.EXIT_DRIFT == 3
    assert e.EXIT_UNKNOWN_ITEM == 4
    assert e.EXIT_MISSING_TARGET == 5
    assert e.EXIT_NOT_A_REPO == 6
    assert e.EXIT_NETWORK == 7
    assert e.EXIT_PERMISSION == 8
    assert e.EXIT_MISSING_DEP == 127


def test_config_is_alias_for_usage():
    # rig spells the usage class EXIT_CONFIG; both names point at the same value (2).
    assert e.EXIT_CONFIG == e.EXIT_USAGE == 2


def test_exit_codes_table_covers_every_constant():
    for code in (
        e.EXIT_OK,
        e.EXIT_INTERNAL,
        e.EXIT_USAGE,
        e.EXIT_DRIFT,
        e.EXIT_UNKNOWN_ITEM,
        e.EXIT_MISSING_TARGET,
        e.EXIT_NOT_A_REPO,
        e.EXIT_NETWORK,
        e.EXIT_PERMISSION,
        e.EXIT_MISSING_DEP,
    ):
        assert code in e.EXIT_CODES
        name, meaning = e.EXIT_CODES[code]
        assert name.startswith("EXIT_")
        assert meaning  # non-empty human description


@pytest.mark.parametrize(
    "cls,code",
    [
        (e.UsageError, 2),
        (e.ConfigError, 2),
        (e.DriftError, 3),
        (e.UnknownItemError, 4),
        (e.MissingTargetError, 5),
        (e.NotARepoError, 6),
        (e.NetworkError, 7),
        (e.PermissionDeniedError, 8),
        (e.MissingDepError, 127),
    ],
)
def test_each_subclass_pins_its_exit_code(cls, code):
    err = cls(what="x")
    assert err.exit_code == code


def test_base_error_defaults_to_internal():
    assert AgentToolError(what="boom").exit_code == e.EXIT_INTERNAL


# --- the error value itself -------------------------------------------------------------
def test_error_is_an_exception_and_str_is_the_what_line():
    err = UsageError(what="bad flag --foo", why="no such flag", fix="see --help")
    assert isinstance(err, Exception)
    assert str(err) == "bad flag --foo"  # str(e) stays terse — only the WHAT


def test_error_is_raisable_and_catchable_as_base():
    with pytest.raises(AgentToolError) as ei:
        raise UsageError(what="nope")
    assert ei.value.exit_code == 2


def test_errors_stay_hashable_like_normal_exceptions():
    # A bare @dataclass would set __hash__ = None; we use eq=False to keep Exception's hash, so
    # downstream code can put errors in sets / dict keys / dedup them.
    err = UsageError(what="x")
    assert hash(err) == hash(err)
    s = {err, UsageError(what="x")}  # must not raise TypeError: unhashable
    assert len(s) == 2  # two distinct instances (identity), not collapsed by value-equality


def test_errors_compare_by_identity_not_value():
    # Identity semantics, like a normal Exception — two errors with equal fields are NOT ==.
    a = UsageError(what="x")
    b = UsageError(what="x")
    assert a == a
    assert a != b


# --- render -----------------------------------------------------------------------------
def test_render_three_part_block_no_color():
    err = UsageError(what="bad flag", why="no such flag", fix="see --help")
    out = render(err, color=False)
    assert out == "error: bad flag\n  why: no such flag\n  fix: see --help"


def test_render_omits_empty_fields():
    out = render(AgentToolError(what="boom"), color=False)
    assert out == "error: boom"
    assert "why:" not in out and "fix:" not in out and "install:" not in out


def test_render_mixed_fields_show_independently():
    # what + fix but no why → the why label is omitted, the fix still shown, order preserved.
    out = render(UsageError(what="w", fix="f"), color=False)
    assert out == "error: w\n  fix: f"
    # what + install but no why/fix
    out2 = render(MissingDepError(what="w", install="brew install x"), color=False)
    assert out2 == "error: w\n  install: brew install x"


def test_render_shows_install_line_when_set():
    err = MissingDepError(
        what="`openscad` is not installed", why="need it", fix="install", install="brew install openscad"
    )
    out = render(err, color=False)
    assert "install: brew install openscad" in out


def test_render_color_wraps_in_ansi():
    out = render(MissingDepError(what="x", why="y", fix="z", install="brew install x"), color=True)
    assert "\033[31m" in out  # red WHAT
    assert "\033[2m" in out  # dim WHY label
    assert "\033[32m" in out  # green FIX
    assert "\033[33m" in out  # yellow INSTALL label
    assert "\033[0m" in out


def test_render_strips_control_chars_from_user_fields():
    # A config value carrying a raw ESC / screen-clear / CR must not reach the terminal verbatim.
    # Stripping the ESC byte (and CR/BEL) neutralizes the sequence — the inert "[2J[H" text that
    # follows is harmless printable characters; only the control BYTES are dangerous.
    nasty = "item\033[2J\033[H\r\x07name"
    out = render(UsageError(what=f"unknown item: {nasty}", why=nasty, fix=nasty), color=False)
    assert "\033" not in out  # no ESC survived → the CSI sequence is defanged
    assert "\r" not in out and "\x07" not in out  # CR and BEL excised
    assert "item" in out and "name" in out  # printable text remains


def test_render_keeps_tab_in_fields():
    # TAB is legitimate whitespace and is preserved (only control/format chars are stripped).
    out = render(UsageError(what="a\tb"), color=False)
    assert "a\tb" in out


def test_render_strips_newline_so_fields_stay_single_line():
    # \n is a C0 control and is stripped — a field rendering as one line is an implicit contract
    # (a multi-line WHY would break the "  why: " label alignment). Pin it.
    out = render(UsageError(what="line1\nline2"), color=False)
    assert "\n" not in out.replace("error: ", "")  # no embedded newline inside the rendered WHAT
    assert "line1line2" in out


def test_render_strips_c1_and_unicode_spoofing_chars():
    # C1 CSI (0x9B), a bidi RTL override (U+202E), and a zero-width space (U+200B) must be removed.
    nasty = "safe" + chr(0x9B) + "path" + chr(0x202E) + "reversed" + chr(0x200B) + "hidden"
    out = render(UsageError(what=nasty), color=False)
    for cp in (0x9B, 0x202E, 0x200B):
        assert chr(cp) not in out
    assert "safe" in out and "path" in out and "hidden" in out

def test_render_sanitizes_before_color_so_own_ansi_survives():
    # Sanitization must run on the FIELD, not the assembled colored line — our own \033[31m must
    # survive while a control char IN the field is still stripped.
    out = render(UsageError(what="bad\x1b[2Jvalue"), color=True)
    assert "\033[31m" in out  # our red WHAT escape survived
    # the field's embedded ESC was stripped (the only ESCs left are our SGR codes, all followed
    # by 'm' or '0m'); assert no raw screen-clear "[2J" preceded by a stray ESC remains
    assert "\x1b[2J" not in out


# --- should_color env precedence --------------------------------------------------------
class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_should_color_no_color_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is False


def test_should_color_no_color_zero_still_disables(monkeypatch):
    # no-color.org: ANY non-empty NO_COLOR disables — "0" is non-empty, so it disables too.
    monkeypatch.setenv("NO_COLOR", "0")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is False


def test_should_color_force_color_overrides_pipe(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert should_color(_FakeStream(False)) is True


def test_should_color_no_arg_uses_stderr(monkeypatch):
    # The default stream (no arg) is sys.stderr — substitute a fake to exercise that branch.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr("agenttools_errors.core.sys.stderr", _FakeStream(True))
    assert should_color() is True
    monkeypatch.setattr("agenttools_errors.core.sys.stderr", _FakeStream(False))
    assert should_color() is False


def test_should_color_tty_only_when_no_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is True
    assert should_color(_FakeStream(False)) is False


def test_should_color_no_color_beats_force_color(monkeypatch):
    # NO_COLOR is checked first — it wins even if FORCE_COLOR is also set.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert should_color(_FakeStream(True)) is False


def test_should_color_empty_no_color_does_not_disable(monkeypatch):
    # no-color.org spec: NO_COLOR must be present AND non-empty. An empty value does NOT disable.
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert should_color(_FakeStream(True)) is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", ""])
def test_should_color_force_color_zero_disables(monkeypatch, val):
    # FORCE_COLOR=0 / false / off forces color OFF (chalk/supports-color convention), even on a TTY.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", val)
    assert should_color(_FakeStream(True)) is False


@pytest.mark.parametrize("val", ["1", "true", "2", "always"])
def test_should_color_force_color_truthy_enables_on_pipe(monkeypatch, val):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", val)
    assert should_color(_FakeStream(False)) is True


# --- guard ------------------------------------------------------------------------------
def test_guard_returns_code_and_prints_for_known_error(capsys):
    def fn() -> int:
        raise UsageError(what="bad", why="because", fix="do x")

    rc = guard(fn, stream=sys.stdout)
    assert rc == 2
    out = capsys.readouterr().out
    assert "error: bad" in out and "why: because" in out


def test_guard_prints_to_stderr_by_default(capsys):
    # The production default stream is stderr (no stream= kwarg) — assert it lands there.
    def fn() -> int:
        raise UsageError(what="boom on stderr")

    rc = guard(fn)
    assert rc == 2
    captured = capsys.readouterr()
    assert "error: boom on stderr" in captured.err
    assert captured.out == ""


def test_guard_honors_force_color(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")  # color even though capsys' stream is not a TTY

    def fn() -> int:
        raise UsageError(what="x")

    guard(fn)
    assert "\033[31m" in capsys.readouterr().err


def test_guard_passes_through_success():
    assert guard(lambda: 0) == 0
    assert guard(lambda: 5) == 5


def test_guard_normalizes_missing_return_to_internal():
    # A command body that forgets `return` (fn() is None) must NOT be reported as success (0).
    assert guard(lambda: None) == e.EXIT_INTERNAL
    # any non-int result is likewise treated as an internal bug, not a 0
    assert guard(lambda: "oops") == e.EXIT_INTERNAL


def test_guard_treats_bool_return_as_internal():
    # bool is a subclass of int; a fn returning True/False is a bug, not exit 1/0.
    assert guard(lambda: True) == e.EXIT_INTERNAL
    assert guard(lambda: False) == e.EXIT_INTERNAL


def test_guard_renders_base_error_exit_1(capsys):
    # The base AgentToolError (exit_code default) goes through guard → exit 1, block printed.
    def fn() -> int:
        raise AgentToolError(what="generic boom")

    rc = guard(fn, stream=sys.stdout)
    assert rc == e.EXIT_INTERNAL
    assert "error: generic boom" in capsys.readouterr().out


def test_guard_does_not_swallow_real_bugs():
    # A non-AgentToolError is a real bug; guard must let it propagate (visible traceback).
    def boom() -> int:
        raise ValueError("a genuine bug")

    with pytest.raises(ValueError, match="a genuine bug"):
        guard(boom)


# --- did_you_mean -----------------------------------------------------------------------
def test_did_you_mean_finds_close_typo():
    assert did_you_mean("reviewr", {"review", "serena", "sverklo"}) == "review"


def test_did_you_mean_rejects_far_token():
    assert did_you_mean("zzzzzzzz", {"review", "serena"}) is None


def test_did_you_mean_empty_candidates():
    assert did_you_mean("anything", set()) is None


def test_did_you_mean_empty_bad_returns_none():
    # No meaningful suggestion for an empty token (a length-1 candidate would otherwise match).
    assert did_you_mean("", {"a", "b"}) is None


def test_did_you_mean_single_char_bad_returns_none():
    # A 1-char token is within distance 1 of every other 1-char candidate → that's noise, not help.
    assert did_you_mean("x", {"a", "z"}) is None
    # but a real 2+ char typo still matches
    assert did_you_mean("li", {"ls", "list"}) == "ls"


def test_did_you_mean_skips_single_char_candidates():
    # A 1-char candidate is too short to be a meaningful suggestion even for a 2-char typo.
    assert did_you_mean("ab", {"a", "zz"}) is None
    # a 2+ char candidate still wins
    assert did_you_mean("ab", {"a", "ac"}) == "ac"


def test_did_you_mean_is_deterministic_on_ties():
    # Two equidistant candidates → alphabetical winner, stable across runs.
    assert did_you_mean("aax", {"aaa", "aab"}) == "aaa"


# --- unknown_item_error heuristics ------------------------------------------------------
def test_unknown_item_did_you_mean_branch():
    err = unknown_item_error(
        category="mcp", bad="reviewr", known={"review", "serena"}, config_path="rig.yaml", key="mcp.items.reviewr"
    )
    assert isinstance(err, UnknownItemError)
    assert err.exit_code == 4
    assert "did you mean `review`" in err.fix
    assert "rig.yaml" in err.fix and "mcp.items.reviewr" in err.fix


def test_unknown_item_empty_catalog_branch():
    err = unknown_item_error(category="mcp", bad="x", known=set(), config_path="rig.yaml")
    assert "no mcp items" in err.why
    assert "remove the `mcp` block" in err.fix
    assert "known: none" not in render(err, color=False)  # never the useless phrasing


def test_unknown_item_fallthrough_lists_known():
    err = unknown_item_error(category="model", bad="qwerty", known={"opus", "sonnet", "haiku"})
    assert "use one of: haiku, opus, sonnet" in err.fix


def test_unknown_item_removed_slot_branch():
    reg = RemovedSlotRegistry().add(
        RemovedSlot(category="mcp", name="review", reason="removed in #32: review is a CLI, not MCP")
    )
    err = unknown_item_error(
        category="mcp", bad="review", known={"serena"}, config_path="rig.yaml", key="mcp.items.review", removed=reg
    )
    assert "removed mcp slot: review" in err.what
    assert "removed in #32" in err.why
    assert "remove `mcp.items.review`" in err.fix


def test_unknown_item_without_config_path_degrades_gracefully():
    # An arg (not a config key): no file path, but the message still makes sense.
    err = unknown_item_error(category="command", bad="lst", known={"list", "show"})
    assert "unknown command item: lst" in err.what
    assert "did you mean `list`" in err.fix
    assert "rig.yaml" not in render(err, color=False)


def test_unknown_item_removed_slot_beats_did_you_mean():
    # Precedence: a name that is BOTH a removed slot AND near a known item → removed-slot wins.
    reg = RemovedSlotRegistry().add(RemovedSlot("mcp", "review", "removed in #32"))
    # "review" is also within edit distance of the known item "preview" — removed must take priority.
    err = unknown_item_error(category="mcp", bad="review", known={"preview"}, removed=reg)
    assert "removed mcp slot: review" in err.what
    assert "did you mean" not in err.fix


def test_unknown_item_rejects_empty_bad():
    # An empty bad name is a caller bug, not a user typo — must raise, not render a stray backtick.
    with pytest.raises(ValueError, match="non-blank"):
        unknown_item_error(category="mcp", bad="", known={"a"})


def test_unknown_item_rejects_whitespace_bad():
    with pytest.raises(ValueError, match="non-blank"):
        unknown_item_error(category="mcp", bad="   ", known={"a"})


def test_unknown_item_rejects_blank_category():
    with pytest.raises(ValueError, match="category must be"):
        unknown_item_error(category="", bad="x", known={"a"})


# --- removed-slot registry ---------------------------------------------------------------
def test_registry_lookup_miss_returns_none():
    reg = RemovedSlotRegistry()
    assert reg.lookup("mcp", "never-existed") is None


def test_registry_add_chains():
    reg = RemovedSlotRegistry()
    out = reg.add(RemovedSlot("mcp", "a", "r"))
    assert out is reg  # chaining
    assert reg.lookup("mcp", "a") is not None


# --- missing_dep_error / require_tool ---------------------------------------------------
def test_missing_dep_error_rejects_blank_dep():
    with pytest.raises(ValueError, match="non-blank"):
        missing_dep_error(dep="", needed_for="x", install="y")


def test_missing_dep_error_carries_install_and_127():
    err = missing_dep_error(
        dep="openscad", needed_for="to produce the mesh", install="brew install openscad", rerun="cli render m.scad"
    )
    assert err.exit_code == 127
    assert err.install == "brew install openscad"
    assert "to produce the mesh" in err.why
    assert "cli render m.scad" in err.fix


def test_require_tool_returns_path_when_present(monkeypatch):
    monkeypatch.setattr("agenttools_errors.core.shutil.which", lambda d: "/usr/bin/" + d)
    assert require_tool("git", needed_for="for x", install="brew install git") == "/usr/bin/git"


def test_require_tool_rejects_empty_dep():
    # Guard against the footgun where which("") → None yields "error: `` is not installed".
    with pytest.raises(ValueError, match="non-empty"):
        require_tool("", needed_for="x", install="y")


def test_require_tool_raises_structured_error_when_missing(monkeypatch):
    monkeypatch.setattr("agenttools_errors.core.shutil.which", lambda d: None)
    with pytest.raises(MissingDepError) as ei:
        require_tool("openscad", needed_for="to render", install="brew install openscad", rerun="3d render")
    err = ei.value
    assert err.exit_code == 127
    assert err.install == "brew install openscad"
    assert "3d render" in err.fix


# --- missing_target_error ---------------------------------------------------------------
def test_missing_target_error_exit_5():
    err = missing_target_error(
        what_kind="hook", target="~/.claude/hooks/dead.json", why="the hook path is gone", regen="rig apply"
    )
    assert err.exit_code == 5
    assert "missing hook: ~/.claude/hooks/dead.json" in err.what
    assert err.why == "the hook path is gone"
    assert err.fix == "rig apply"


# --- not_a_repo_error -------------------------------------------------------------------
def test_not_a_repo_error_exit_6():
    err = not_a_repo_error(command="rig apply", cwd="/tmp/x")
    assert err.exit_code == 6
    assert "rig apply" in err.what
    assert "/tmp/x" in err.why


def test_not_a_repo_error_without_cwd():
    err = not_a_repo_error(command="rig apply")
    assert err.exit_code == 6
    assert err.why == "no git repository found"  # no trailing "(... )" when cwd omitted


# --- pickle round-trip (structured payload survives a process boundary) -----------------
def test_error_pickle_round_trips_all_fields():
    err = MissingDepError(
        what="`openscad` is not installed",
        why="need it for the mesh",
        fix="install it, then re-run",
        install="brew install openscad",
    )
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is MissingDepError
    assert restored.what == err.what
    assert restored.why == err.why
    assert restored.fix == err.fix
    assert restored.install == err.install
    assert restored.exit_code == 127  # the class default survives, not reset to EXIT_INTERNAL
    assert str(restored) == err.what  # args still set so str(e) stays the WHAT


def test_error_pickle_preserves_overridden_exit_code():
    # A base error with an explicit non-default exit_code must keep it across pickling.
    err = AgentToolError(what="x", exit_code=42)
    assert pickle.loads(pickle.dumps(err)).exit_code == 42


def test_error_copy_round_trips_all_fields():
    # copy.copy / deepcopy also consult __reduce__; the structured payload must survive both.
    err = MissingDepError(what="w", why="y", fix="f", install="i")
    for clone in (copy.copy(err), copy.deepcopy(err)):
        assert type(clone) is MissingDepError
        assert (clone.what, clone.why, clone.fix, clone.install, clone.exit_code) == (
            "w",
            "y",
            "f",
            "i",
            127,
        )


def test_error_reduce_introspects_all_dataclass_fields():
    # __reduce__ pulls fields from dataclasses.fields(), so the round-trip dict covers every
    # field — not a hardcoded five. Assert it matches the live field set (a future added field
    # is automatically preserved).
    from dataclasses import fields

    err = MissingDepError(what="w", why="y", fix="f", install="i")
    reduced = err.__reduce__()
    state = reduced[2]
    assert set(state) == {f.name for f in fields(err)}


# --- PermissionDeniedError name (public-contract surface) -------------------------------
def test_permission_denied_error_has_clean_name_and_code():
    err = PermissionDeniedError(what="403 from the API")
    assert type(err).__name__ == "PermissionDeniedError"  # no leaked internal "PermissionError_"
    assert err.exit_code == 8
    # name survives pickling too (tracebacks/Sentry show the contract name)
    assert type(pickle.loads(pickle.dumps(err))).__name__ == "PermissionDeniedError"


def test_permission_denied_error_does_not_shadow_builtin():
    # Defining our class must not have rebound the builtin PermissionError.
    assert PermissionError is not PermissionDeniedError


# --- did_you_mean length prefilter ------------------------------------------------------
def test_did_you_mean_length_prefilter_still_finds_match():
    # A big catalog with one close name: the prefilter skips far-length candidates but keeps the match.
    catalog = {f"item{i:04d}" for i in range(500)} | {"review"}
    assert did_you_mean("reviewr", catalog) == "review"


def test_did_you_mean_prefilter_rejects_length_far_token():
    # "x" vs many long names — length differs by > 3 for all, so nothing matches.
    assert did_you_mean("x", {"reviewing", "serenading"}) is None


def test_unknown_item_removed_slot_without_key():
    # The removed-slot branch with no config key/path (an arg, not a config entry).
    reg = RemovedSlotRegistry().add(RemovedSlot("mcp", "review", "removed in #32: review is a CLI"))
    err = unknown_item_error(category="mcp", bad="review", known={"serena"}, removed=reg)
    assert "removed mcp slot: review" in err.what
    assert "stop passing `review`" in err.fix
    assert err.exit_code == 4


# --- public surface ---------------------------------------------------------------------
def test_all_exports_importable():
    for name in e.__all__:
        assert hasattr(e, name), f"{name} listed in __all__ but missing"


def test_version_present():
    assert isinstance(e.__version__, str) and e.__version__
