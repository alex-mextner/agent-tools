"""Tests for agenttools_tmux_inject — inject text/keys into a tmux pane/session.

No real tmux is ever invoked: the tmux binary resolver and the subprocess wrapper are
monkeypatched so the suite is hermetic, deterministic, and network/tmux-free. Run from the
repo root::

    uv run --with pytest python -m pytest tests/ -q
    # or, if agenttools-tmux-inject is installed:  python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_tmux_inject as ati  # noqa: E402
from agenttools_tmux_inject import (  # noqa: E402
    ERR_BAD_TARGET,
    ERR_NO_SERVER,
    ERR_NO_TMUX,
    ERR_SEND_FAILED,
    ERR_TIMEOUT,
    InjectResult,
    Target,
    has_session,
    inject,
    list_panes,
    resolve_target,
    send_keys,
)
from agenttools_tmux_inject import core  # noqa: E402

_FAKE_TMUX = "/usr/bin/tmux"


class _Recorder:
    """Stand-in for ``core._run``: records every argv and replays scripted results.

    Each call pops the next scripted ``CompletedProcess`` (or uses a default success). The
    full argv list of every invocation is retained in ``calls`` for exact assertions.
    """

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._results = list(results or [])

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        if self._results:
            result = self._results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Pristine env: no AGENTTOOLS_TMUX_BIN leaking in; HOME-independent by construction."""
    monkeypatch.delenv("AGENTTOOLS_TMUX_BIN", raising=False)
    yield


@pytest.fixture
def tmux_present(monkeypatch):
    """tmux resolves to a fake path; return a fresh _Recorder wired into core._run."""

    def _factory(results=None):
        rec = _Recorder(results)
        monkeypatch.setattr(core, "_which_tmux", lambda: _FAKE_TMUX)
        monkeypatch.setattr(core, "_run", rec)
        return rec

    return _factory


@pytest.fixture
def tmux_absent(monkeypatch):
    """tmux is not on PATH; _run is wired to explode if (wrongly) called."""

    def _boom(*_a, **_k):  # pragma: no cover - asserts it's never reached
        raise AssertionError("subprocess must not run when tmux is absent")

    monkeypatch.setattr(core, "_which_tmux", lambda: None)
    monkeypatch.setattr(core, "_run", _boom)


# --- target resolution ------------------------------------------------------------------


def test_resolve_pane_id():
    t = resolve_target("%12")
    assert t.is_pane_id is True
    assert t.raw == "%12"
    assert t.session is None and t.window is None and t.pane is None
    assert t.as_tmux_arg() == "%12"


def test_resolve_session_window_pane():
    t = resolve_target("work:1.0")
    assert t.is_pane_id is False
    assert t.session == "work"
    assert t.window == "1"
    assert t.pane == "0"
    assert t.as_tmux_arg() == "work:1.0"


def test_resolve_session_only():
    t = resolve_target("work")
    assert t.session == "work"
    assert t.window is None and t.pane is None
    assert t.is_pane_id is False


def test_resolve_session_window_no_pane():
    t = resolve_target("work:2")
    assert t.session == "work"
    assert t.window == "2"
    assert t.pane is None


def test_resolve_leading_colon_keeps_session_none():
    t = resolve_target(":1.0")
    assert t.session is None
    assert t.window == "1"
    assert t.pane == "0"
    assert t.as_tmux_arg() == ":1.0"


def test_resolve_passes_through_existing_target():
    src = Target(raw="%7", is_pane_id=True)
    assert resolve_target(src) is src


def test_resolve_rejects_non_string():
    with pytest.raises(TypeError):
        resolve_target(123)  # type: ignore[arg-type]


def test_resolve_rejects_empty():
    with pytest.raises(ValueError):
        resolve_target("   ")


def test_pane_id_with_nondigits_is_not_pane_id():
    # '%abc' isn't a real pane id; treat it as a (weird) session name, raw preserved.
    t = resolve_target("%abc")
    assert t.is_pane_id is False
    assert t.raw == "%abc"


# --- inject: exact argv, literal default, enter behavior --------------------------------


def test_inject_default_argv_literal_text_then_enter(tmux_present):
    rec = tmux_present()
    res = inject("work:1.0", "done, X unblocked")

    assert res.ok is True
    assert bool(res) is True
    # Two send-keys calls: literal text, then an interpreted Enter.
    assert rec.calls == [
        [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "--", "done, X unblocked"],
        [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "Enter"],
    ]
    # The result's argv records both.
    assert list(res.argv) == [
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "--", "done, X unblocked",
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "Enter",
    ]
    assert res.error is None


def test_inject_enter_false_sends_only_text(tmux_present):
    rec = tmux_present()
    res = inject("%3", "staged command", enter=False)

    assert res.ok is True
    assert rec.calls == [
        [_FAKE_TMUX, "send-keys", "-t", "%3", "-l", "--", "staged command"],
    ]
    # No interpreted Enter call at all.
    assert all("Enter" not in c for c in rec.calls)


def test_inject_literal_false_drops_dash_l_and_still_enters(tmux_present):
    rec = tmux_present()
    res = inject("work:0.0", "C-c", literal=False)

    assert res.ok is True
    # No '-l' flag when literal=False; '--' still terminates options.
    assert rec.calls[0] == [
        _FAKE_TMUX, "send-keys", "-t", "work:0.0", "--", "C-c",
    ]
    assert rec.calls[1] == [_FAKE_TMUX, "send-keys", "-t", "work:0.0", "Enter"]


def test_inject_message_starting_with_dash_is_after_double_dash(tmux_present):
    rec = tmux_present()
    inject("s:0.0", "-rf /tmp", enter=False)
    # '--' guarantees a leading-dash message is never parsed as a flag.
    argv = rec.calls[0]
    assert argv[-2] == "--"
    assert argv[-1] == "-rf /tmp"


def test_inject_preserves_unicode_and_spaces_verbatim(tmux_present):
    rec = tmux_present()
    msg = "готово: деплой разблокирован ✅"
    inject("s", msg, enter=False)
    assert rec.calls[0][-1] == msg


# --- send_keys: low-level primitive defaults --------------------------------------------


def test_send_keys_defaults_no_enter_literal(tmux_present):
    rec = tmux_present()
    res = send_keys("%5", "hello")
    assert res.ok is True
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "%5", "-l", "--", "hello"]]


def test_send_keys_enter_true_adds_interpreted_return(tmux_present):
    rec = tmux_present()
    send_keys("%5", "hello", enter=True)
    assert rec.calls[1] == [_FAKE_TMUX, "send-keys", "-t", "%5", "Enter"]


def test_send_keys_interpreted_keyname(tmux_present):
    rec = tmux_present()
    send_keys("%5", "Escape", literal=False)
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "%5", "--", "Escape"]]


def test_literal_false_enter_false_combo_exact_argv(tmux_present):
    # The one combination not covered by the single-flag tests: no -l, no Enter.
    rec = tmux_present()
    res = inject("work:1.0", "C-c", literal=False, enter=False)
    assert res.ok is True
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "work:1.0", "--", "C-c"]]


def test_timeout_is_plumbed_through_to_run(tmux_present):
    rec = tmux_present()
    inject("work:1.0", "x", timeout=2.5)
    # Both the text and the Enter send must receive the caller's timeout.
    assert rec.timeouts == [2.5, 2.5]


def test_send_keys_timeout_plumbed_through(tmux_present):
    rec = tmux_present()
    send_keys("%5", "x", timeout=1.0)
    assert rec.timeouts == [1.0]


def test_send_keys_rejects_non_string_keys(tmux_present):
    tmux_present()
    with pytest.raises(TypeError):
        send_keys("%5", 42)  # type: ignore[arg-type]


def test_inject_rejects_non_string_text(tmux_present):
    tmux_present()
    with pytest.raises(TypeError):
        inject("%5", None)  # type: ignore[arg-type]


def test_inject_rejects_bad_target(tmux_present):
    tmux_present()
    with pytest.raises(ValueError):
        inject("", "x")


# --- tmux-absent path -------------------------------------------------------------------


def test_inject_tmux_absent_returns_result_never_raises(tmux_absent):
    res = inject("work:1.0", "done")
    assert isinstance(res, InjectResult)
    assert res.ok is False
    assert bool(res) is False
    assert res.error == ERR_NO_TMUX
    assert "tmux" in res.message.lower()
    # argv is still populated (what WOULD have run), with the resolved tmux name.
    assert res.argv[:2] == ("tmux", "send-keys")
    assert res.target == "work:1.0"


def test_send_keys_tmux_absent(tmux_absent):
    res = send_keys("%1", "x")
    assert res.ok is False
    assert res.error == ERR_NO_TMUX


def test_has_session_false_when_tmux_absent(tmux_absent):
    assert has_session("work") is False


def test_list_panes_empty_when_tmux_absent(tmux_absent):
    assert list_panes() == []
    assert list_panes("work") == []


# --- failure classification -------------------------------------------------------------


def test_inject_no_server_running_classified(tmux_present):
    tmux_present([_proc(returncode=1, stderr="no server running on /tmp/tmux-1000/default")])
    res = inject("work:1.0", "x")
    assert res.ok is False
    assert res.error == ERR_NO_SERVER
    assert res.returncode == 1
    assert "no server" in res.message.lower()


def test_inject_bad_target_classified(tmux_present):
    tmux_present([_proc(returncode=1, stderr="can't find pane: work:9.9")])
    res = inject("work:9.9", "x")
    assert res.ok is False
    assert res.error == ERR_BAD_TARGET


def test_inject_generic_nonzero_is_send_failed(tmux_present):
    tmux_present([_proc(returncode=3, stderr="some other tmux error")])
    res = inject("work:1.0", "x")
    assert res.ok is False
    assert res.error == ERR_SEND_FAILED
    assert res.returncode == 3


def test_text_send_ok_but_enter_fails_reports_failure(tmux_present):
    # First call (text) succeeds, second (Enter) fails -> overall not ok.
    tmux_present([_proc(returncode=0), _proc(returncode=1, stderr="can't find pane")])
    res = inject("work:1.0", "hi")
    assert res.ok is False
    # The combined argv records BOTH the successful text send and the failed Enter send.
    assert list(res.argv) == [
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "--", "hi",
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "Enter",
    ]
    # The failure is classified from the Enter call's stderr.
    assert res.error == ERR_BAD_TARGET
    assert res.returncode == 1


def test_inject_timeout_classified_as_err_timeout(tmux_present):
    import subprocess

    tmux_present([subprocess.TimeoutExpired(cmd="tmux", timeout=5.0)])
    res = inject("work:1.0", "x")
    assert res.ok is False
    assert res.error == ERR_TIMEOUT


def test_enter_timeout_classified_as_err_timeout(tmux_present):
    import subprocess

    # Text send OK, Enter send hangs -> ERR_TIMEOUT with both argvs recorded.
    tmux_present([_proc(returncode=0), subprocess.TimeoutExpired(cmd="tmux", timeout=5.0)])
    res = inject("work:1.0", "x")
    assert res.ok is False
    assert res.error == ERR_TIMEOUT
    assert any("Enter" in part for part in res.argv)


def test_subprocess_raising_is_caught(tmux_present, monkeypatch):
    def _raise(argv, *, timeout):
        raise OSError("exec format error")

    monkeypatch.setattr(core, "_which_tmux", lambda: _FAKE_TMUX)
    monkeypatch.setattr(core, "_run", _raise)
    res = inject("work:1.0", "x")
    assert res.ok is False
    assert res.error == ERR_SEND_FAILED
    assert "exec format error" in res.message


# --- has_session ------------------------------------------------------------------------


def test_has_session_true_on_exit_zero(tmux_present):
    rec = tmux_present([_proc(returncode=0)])
    assert has_session("work") is True
    assert rec.calls == [[_FAKE_TMUX, "has-session", "-t", "work"]]


def test_has_session_false_on_nonzero(tmux_present):
    tmux_present([_proc(returncode=1, stderr="can't find session: nope")])
    assert has_session("nope") is False


def test_has_session_rejects_empty_name(tmux_present):
    tmux_present()
    with pytest.raises(ValueError):
        has_session("")


def test_has_session_swallows_subprocess_error(monkeypatch):
    monkeypatch.setattr(core, "_which_tmux", lambda: _FAKE_TMUX)

    def _raise(argv, *, timeout):
        raise OSError("boom")

    monkeypatch.setattr(core, "_run", _raise)
    assert has_session("work") is False


def test_has_session_strips_name_before_passing_to_tmux(tmux_present):
    # The validated name and the queried name must agree — surrounding whitespace stripped.
    rec = tmux_present([_proc(returncode=0)])
    assert has_session("  work  ") is True
    assert rec.calls == [[_FAKE_TMUX, "has-session", "-t", "work"]]


# --- list_panes -------------------------------------------------------------------------


def test_list_panes_parses_format_output(tmux_present):
    out = (
        "%0\twork\t1\teditor\t0\t1\tvim\n"
        "%1\twork\t1\teditor\t1\t0\tbash\n"
    )
    rec = tmux_present([_proc(returncode=0, stdout=out)])
    panes = list_panes()
    assert rec.calls[0][:2] == [_FAKE_TMUX, "list-panes"]
    assert "-F" in rec.calls[0]
    assert panes == [
        {
            "pane_id": "%0",
            "session": "work",
            "window_index": "1",
            "window_name": "editor",
            "pane_index": "0",
            "active": True,
            "title": "vim",
        },
        {
            "pane_id": "%1",
            "session": "work",
            "window_index": "1",
            "window_name": "editor",
            "pane_index": "1",
            "active": False,
            "title": "bash",
        },
    ]


def test_list_panes_scopes_session_with_dash_s(tmux_present):
    rec = tmux_present([_proc(returncode=0, stdout="")])
    list_panes("work")
    argv = rec.calls[0]
    # A bare session target gets '-s' (whole-session scope) plus '-t work'.
    assert "-s" in argv
    assert argv[-2:] == ["-t", "work"]


def test_list_panes_pane_target_no_dash_s(tmux_present):
    rec = tmux_present([_proc(returncode=0, stdout="")])
    list_panes("%3")
    argv = rec.calls[0]
    assert "-s" not in argv
    assert argv[-2:] == ["-t", "%3"]


def test_list_panes_window_target_no_dash_s(tmux_present):
    # session+window set (pane None) -> NOT whole-session scope, so no '-s'.
    rec = tmux_present([_proc(returncode=0, stdout="")])
    list_panes("work:1")
    argv = rec.calls[0]
    assert "-s" not in argv
    assert argv[-2:] == ["-t", "work:1"]


def test_list_panes_no_target_uses_dash_a_for_all_panes(tmux_present):
    rec = tmux_present([_proc(returncode=0, stdout="")])
    list_panes()
    argv = rec.calls[0]
    assert "-t" not in argv
    assert "-s" not in argv
    assert "-F" in argv
    # No target means "every pane on the server" — without '-a' tmux scopes list-panes to
    # the current window only, which contradicts the documented "omit for all panes".
    assert "-a" in argv


def test_list_panes_empty_on_nonzero(tmux_present):
    tmux_present([_proc(returncode=1, stderr="no server running")])
    assert list_panes() == []


def test_list_panes_tolerates_short_rows(tmux_present):
    # Older tmux emitting fewer fields must not blow up — missing fields fill blank.
    tmux_present([_proc(returncode=0, stdout="%0\twork\n")])
    panes = list_panes()
    assert panes[0]["pane_id"] == "%0"
    assert panes[0]["session"] == "work"
    assert panes[0]["title"] == ""
    assert panes[0]["active"] is False


# --- env override for the tmux binary ---------------------------------------------------


def test_env_override_changes_resolved_binary_name_when_absent(monkeypatch):
    # With a custom bare name that isn't on PATH, the absent-path message names it.
    monkeypatch.setenv("AGENTTOOLS_TMUX_BIN", "tmux-custom")
    monkeypatch.setattr(core, "_run", lambda *a, **k: _proc())  # never reached anyway
    # Force which to behave as "not found" for the custom name.
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = inject("s:0.0", "x")
    assert res.error == ERR_NO_TMUX
    assert "tmux-custom" in res.message
    # The would-be argv uses the configured name too.
    assert res.argv[0] == "tmux-custom"


def test_env_override_absolute_path_used_directly(monkeypatch, tmp_path):
    # An absolute AGENTTOOLS_TMUX_BIN pointing at a real executable is used verbatim,
    # bypassing PATH lookup. Exercises the explicit-path branch of _which_tmux.
    import os
    import shutil

    stub = tmp_path / "tmux-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("AGENTTOOLS_TMUX_BIN", str(stub))
    # which must NOT be consulted for an absolute path; blow up if it is.
    monkeypatch.setattr(
        shutil, "which", lambda name: pytest.fail("which should not run for abs path")
    )

    recorded = {}

    def _run(argv, *, timeout):
        recorded["argv"] = list(argv)
        return _proc(returncode=0)

    monkeypatch.setattr(core, "_run", _run)
    res = inject("s:0.0", "x", enter=False)
    assert res.ok is True
    assert recorded["argv"][0] == str(stub)


def test_env_override_absolute_path_missing_reports_no_tmux(monkeypatch, tmp_path):
    # An absolute path that doesn't exist resolves to "no tmux", not a PATH fallback.
    missing = tmp_path / "nope" / "tmux"
    monkeypatch.setenv("AGENTTOOLS_TMUX_BIN", str(missing))
    res = inject("s:0.0", "x")
    assert res.error == ERR_NO_TMUX


def test_inject_result_bool_contract():
    assert bool(InjectResult(ok=True)) is True
    assert bool(InjectResult(ok=False)) is False
    assert InjectResult(ok=True)
    assert not InjectResult(ok=False)


# --- package surface --------------------------------------------------------------------


def test_public_exports_present():
    for name in (
        "inject",
        "send_keys",
        "has_session",
        "list_panes",
        "resolve_target",
        "InjectResult",
        "Target",
        "ERR_NO_TMUX",
        "ERR_NO_SERVER",
        "ERR_BAD_TARGET",
        "ERR_SEND_FAILED",
        "ERR_TIMEOUT",
    ):
        assert hasattr(ati, name), f"missing public export {name!r}"
    assert ati.__version__
