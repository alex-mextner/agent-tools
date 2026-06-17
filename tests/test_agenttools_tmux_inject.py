"""Tests for agenttools_tmux_inject — inject text/keys into a tmux pane/session.

No real tmux is ever invoked: the tmux binary resolver and the subprocess wrapper are
monkeypatched so the suite is hermetic, deterministic, and network/tmux-free. Run from the
repo root::

    uv run --with pytest python -m pytest tests/ -q
    # or, if agenttools-tmux-inject is installed:  python -m pytest tests/ -q
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
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
    # Two send-keys calls: literal text (empty key arg guards a dash-leading payload —
    # tmux has no "--" marker), then an interpreted Enter.
    assert rec.calls == [
        [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "", "done, X unblocked"],
        [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "Enter"],
    ]
    # The result's argv records both.
    assert list(res.argv) == [
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "", "done, X unblocked",
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "Enter",
    ]
    # No unsupported "--" end-of-options marker is ever emitted.
    assert "--" not in rec.calls[0]
    assert res.error is None


def test_inject_enter_false_sends_only_text(tmux_present):
    rec = tmux_present()
    res = inject("%3", "staged command", enter=False)

    assert res.ok is True
    assert rec.calls == [
        [_FAKE_TMUX, "send-keys", "-t", "%3", "-l", "", "staged command"],
    ]
    # No interpreted Enter call at all.
    assert all("Enter" not in c for c in rec.calls)


def test_inject_literal_false_drops_dash_l_and_still_enters(tmux_present):
    rec = tmux_present()
    res = inject("work:0.0", "C-c", literal=False)

    assert res.ok is True
    # No '-l' flag when literal=False; an empty key arg still guards a dash-leading payload.
    assert rec.calls[0] == [
        _FAKE_TMUX, "send-keys", "-t", "work:0.0", "", "C-c",
    ]
    assert rec.calls[1] == [_FAKE_TMUX, "send-keys", "-t", "work:0.0", "Enter"]


def test_inject_message_starting_with_dash_is_guarded_not_misparsed(tmux_present):
    rec = tmux_present()
    res = inject("s:0.0", "-rf /tmp", enter=False)
    # A leading-dash message must never be parsed as a flag. tmux send-keys has NO "--"
    # end-of-options marker (optstring 'c:FHKlMN:Rt:X'); an empty leading key arg guards it.
    argv = rec.calls[0]
    assert "--" not in argv, "tmux send-keys does not accept a '--' marker"
    assert argv[-2] == "", "empty key arg must immediately precede the dash-leading payload"
    assert argv[-1] == "-rf /tmp"
    assert res.ok is True


def test_inject_done_is_ok_and_delivers_text_no_double_dash(tmux_present):
    # Acceptance: inject('done') -> ok=True and the literal text reaches tmux, with NO '--'.
    rec = tmux_present()
    res = inject("work:1.0", "done")
    assert res.ok is True
    text_argv = rec.calls[0]
    assert text_argv == [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "", "done"]
    assert "--" not in text_argv
    # The payload itself is the last arg, delivered verbatim.
    assert text_argv[-1] == "done"


def test_inject_help_payload_delivered_literally(tmux_present):
    # Acceptance: text starting with '-' (e.g. '--help') is delivered literally as the key
    # payload, never misparsed as a tmux flag, and the call succeeds.
    rec = tmux_present()
    res = inject("work:1.0", "--help", enter=False)
    assert res.ok is True
    argv = rec.calls[0]
    assert "--" not in argv
    assert argv == [_FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "", "--help"]
    assert argv[-1] == "--help"


def test_no_double_dash_marker_in_option_region(tmux_present):
    # The unsupported "--" end-of-options marker must never appear in the OPTION region of a
    # send-keys argv (everything before the trailing payload), across both the literal and
    # interpreted paths. Asserting on the option region — not the whole argv — is deliberate:
    # "--" is a perfectly valid *payload* (see the next test), so a blanket ban would be wrong.
    rec = tmux_present()
    inject("w:0.0", "--help", enter=False)  # literal path
    inject("w:0.0", "-x", literal=False, enter=False)  # interpreted path
    for call in rec.calls:
        option_region = call[:-1]  # drop the payload (last arg)
        assert "--" not in option_region


def test_inject_literal_double_dash_payload_is_delivered_verbatim(tmux_present):
    # "--" is a legitimate payload to type literally; it must be sent as the key argument,
    # guarded by the empty leading arg, NOT swallowed as an option-terminator. The empty
    # guard arg is what makes this unambiguous to tmux's parser.
    rec = tmux_present()
    res = inject("w:0.0", "--", enter=False)
    assert res.ok is True
    assert rec.calls[0] == [_FAKE_TMUX, "send-keys", "-t", "w:0.0", "-l", "", "--"]
    # The literal "--" is the PAYLOAD (last arg), not an option marker in the middle.
    assert rec.calls[0][-1] == "--"
    assert "--" not in rec.calls[0][:-1]


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
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "%5", "-l", "", "hello"]]


def test_send_keys_enter_true_adds_interpreted_return(tmux_present):
    rec = tmux_present()
    send_keys("%5", "hello", enter=True)
    assert rec.calls[1] == [_FAKE_TMUX, "send-keys", "-t", "%5", "Enter"]


def test_send_keys_interpreted_keyname(tmux_present):
    rec = tmux_present()
    send_keys("%5", "Escape", literal=False)
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "%5", "", "Escape"]]


def test_literal_false_enter_false_combo_exact_argv(tmux_present):
    # The one combination not covered by the single-flag tests: no -l, no Enter.
    rec = tmux_present()
    res = inject("work:1.0", "C-c", literal=False, enter=False)
    assert res.ok is True
    assert rec.calls == [[_FAKE_TMUX, "send-keys", "-t", "work:1.0", "", "C-c"]]


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
        _FAKE_TMUX, "send-keys", "-t", "work:1.0", "-l", "", "hi",
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


# --- real-tmux integration (end-to-end delivery proof) ----------------------------------
#
# The bug this guards: a "--" end-of-options marker in the send-keys argv is NOT supported by
# tmux (documented synopsis `send-keys [-FHKlMRX] ... key ...`; optstring 'c:FHKlMN:Rt:X';
# issue #4408), so the previous build made EVERY inject fail on affected tmux versions. The
# authoritative, version-independent regression gate is the mocked argv-shape suite above
# (it asserts no "--" is ever emitted). THESE tests are the complementary end-to-end proof
# that inject('done') and a dash-leading payload ('--help') actually reach a pane.
#
# OPT-IN: the rest of the suite is hermetic by design (see module docstring — "No real tmux
# is ever invoked"), and tmux behaviour around "--" is version-dependent (3.5a tolerates it,
# others reject it), so a real-tmux assertion is a flaky, version-coupled CI signal. These
# run only when ATI_REAL_TMUX_TESTS is set (locally / a dedicated integration job), never in
# the default `pytest tests/` gate. Also skipped if tmux isn't on PATH.


_REAL_TMUX_ENV = "ATI_REAL_TMUX_TESTS"
_TMUX_CMD_TIMEOUT = 10  # seconds for a single helper tmux invocation
_PANE_POLL_TIMEOUT = 3.0  # seconds to wait for injected text to echo into the pane


def _real_tmux_enabled() -> bool:
    return bool(os.environ.get(_REAL_TMUX_ENV)) and shutil.which("tmux") is not None


real_tmux = pytest.mark.skipif(
    not _real_tmux_enabled(),
    reason=f"set {_REAL_TMUX_ENV}=1 (and have tmux on PATH) to run real-tmux integration",
)


def _wait_for_pane(capture, needle: str, timeout: float = _PANE_POLL_TIMEOUT) -> str:
    """Poll ``capture()`` until ``needle`` appears or ``timeout`` elapses; return last seen."""
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        captured = capture()
        if needle in captured:
            return captured
        time.sleep(0.1)
    return captured


@pytest.fixture
def live_pane(monkeypatch, tmp_path):
    """A real, throwaway tmux session running a line-echo loop; yields its session target.

    The pane runs a ``while read`` loop that echoes ``GOT[<line>]`` for each line we inject
    (text + Enter), so delivery is verifiable via ``capture-pane``. The server is bound to a
    private socket (never the user's real one) and torn down on exit. The library is pointed
    at a tiny shim that prepends ``-L <socket>`` so ``inject()`` talks to that same server;
    ``core._run``/``_which_tmux`` are left UNMOCKED — this is the genuine live path.
    """
    tmux = shutil.which("tmux")
    socket = f"ati-test-{uuid.uuid4().hex[:8]}"
    session = "live"

    def _tmux(*args, check=True):
        return subprocess.run(
            [tmux, "-L", socket, *args],
            capture_output=True,
            text=True,
            timeout=_TMUX_CMD_TIMEOUT,
            check=check,
        )

    # A shim that scopes the library's `tmux send-keys ...` to our private socket. shlex.quote
    # the interpolated values so a path/socket with odd characters can't break the script.
    shim = tmp_path / "tmux"
    shim.write_text(
        f'#!/bin/sh\nexec {shlex.quote(tmux)} -L {shlex.quote(socket)} "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("AGENTTOOLS_TMUX_BIN", str(shim))

    _tmux(
        "new-session", "-d", "-s", session, "-x", "120", "-y", "12",
        "sh", "-c", 'while IFS= read -r x; do printf "GOT[%s]\\n" "$x"; done',
    )
    try:
        yield session, _tmux
    finally:
        _tmux("kill-server", check=False)


@real_tmux
def test_real_inject_done_is_ok_and_delivered(live_pane):
    session, _tmux = live_pane
    res = inject(session, "done")
    assert res.ok is True, f"inject failed: {res.error} {res.message!r} argv={res.argv}"
    captured = _wait_for_pane(
        lambda: _tmux("capture-pane", "-t", session, "-p").stdout, "GOT[done]"
    )
    assert "GOT[done]" in captured, f"text not delivered; pane:\n{captured}"


@real_tmux
def test_real_inject_leading_dash_payload_delivered_literally(live_pane):
    session, _tmux = live_pane
    # The exact regression: a payload starting with '-' used to be misparsed as a flag.
    res = inject(session, "--help")
    assert res.ok is True, f"inject failed: {res.error} {res.message!r} argv={res.argv}"
    captured = _wait_for_pane(
        lambda: _tmux("capture-pane", "-t", session, "-p").stdout, "GOT[--help]"
    )
    assert "GOT[--help]" in captured, f"dash-leading text not delivered; pane:\n{captured}"
