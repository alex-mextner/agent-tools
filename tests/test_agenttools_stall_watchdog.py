"""Tests for agenttools_stall_watchdog — tiered WARN/ABORT stall detection by transcript mtime.

Unit tests below drive `classify`/`Watchdog` with an injected fake clock and fake mtime
source: no real files, no real sleeps, deterministic threshold-crossing assertions (same
seam discipline as `agenttools_daemon`'s fake spawner/clock). `actions` tests monkeypatch
`subprocess.run` / `agenttools_tmux_inject.inject` / `os.kill` so no real tmux, `tg`
process, or OS signal ever fires. `cli` tests drive `run_watch` with `--once` and a
monkeypatched `time` module so the loop body is exercised without real sleeps.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_stall_watchdog.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_stall_watchdog import cli as asw_cli  # noqa: E402
from agenttools_stall_watchdog import actions as asw_actions  # noqa: E402
from agenttools_stall_watchdog import core as asw_core  # noqa: E402


# --- fakes --------------------------------------------------------------------------------


class FakeClock:
    """A controllable clock: `.advance(n)` moves time forward; calling it reads the value."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class FakeMtimeSource:
    """A controllable transcript mtime source: `.touch(t)` sets the mtime; `.remove()` clears it."""

    def __init__(self) -> None:
        self._mtime = None

    def touch(self, t: float) -> None:
        self._mtime = t

    def remove(self) -> None:
        self._mtime = None

    def __call__(self, path: str) -> float | None:
        return self._mtime


# --- core.classify --------------------------------------------------------------------------


class TestClassify:
    def test_below_warn_is_ok(self):
        assert asw_core.classify(299.0, warn_after=300, abort_after=1800) == "ok"

    def test_at_warn_threshold_is_warn(self):
        assert asw_core.classify(300.0, warn_after=300, abort_after=1800) == "warn"

    def test_between_warn_and_abort_is_warn(self):
        assert asw_core.classify(1000.0, warn_after=300, abort_after=1800) == "warn"

    def test_at_abort_threshold_is_abort(self):
        assert asw_core.classify(1800.0, warn_after=300, abort_after=1800) == "abort"

    def test_past_abort_is_abort(self):
        assert asw_core.classify(999999.0, warn_after=300, abort_after=1800) == "abort"

    def test_misconfigured_thresholds_still_resolve_to_the_more_severe_tier(self):
        # abort_after <= warn_after is a misconfiguration classify() doesn't reject, but it
        # must not get stuck reporting "warn" forever past the (lower) abort threshold.
        assert asw_core.classify(50.0, warn_after=100, abort_after=10) == "abort"


# --- core.Watchdog ---------------------------------------------------------------------------


class TestWatchdog:
    def _wd(self, clock, mtimes, warn_after=300.0, abort_after=1800.0):
        return asw_core.Watchdog(
            "/fake/transcript.jsonl",
            warn_after=warn_after,
            abort_after=abort_after,
            clock=clock,
            get_mtime=mtimes,
        )

    def test_no_crossing_while_fresh(self):
        clock, mtimes = FakeClock(0.0), FakeMtimeSource()
        mtimes.touch(0.0)
        wd = self._wd(clock, mtimes)
        assert wd.poll() is None
        clock.advance(100)
        assert wd.poll() is None
        assert wd.current_tier == "ok"

    def test_fires_warn_exactly_once_at_threshold(self):
        clock, mtimes = FakeClock(0.0), FakeMtimeSource()
        mtimes.touch(0.0)
        wd = self._wd(clock, mtimes)
        clock.advance(300)
        event = wd.poll()
        assert event is not None
        assert event.tier == "warn"
        assert event.elapsed == 300
        assert event.recovered is False
        # Still stale on the next poll — must NOT re-fire (edge-triggered, not level).
        clock.advance(10)
        assert wd.poll() is None

    def test_fires_abort_after_warn(self):
        clock, mtimes = FakeClock(0.0), FakeMtimeSource()
        mtimes.touch(0.0)
        wd = self._wd(clock, mtimes)
        clock.advance(300)
        warn_event = wd.poll()
        assert warn_event.tier == "warn"
        clock.advance(1500)  # total elapsed 1800 -> abort threshold
        abort_event = wd.poll()
        assert abort_event is not None
        assert abort_event.tier == "abort"
        assert abort_event.elapsed == 1800
        # Still stale on the next poll — must NOT re-fire.
        clock.advance(10)
        assert wd.poll() is None

    def test_progress_resets_the_episode_and_a_later_stall_refires(self):
        clock, mtimes = FakeClock(0.0), FakeMtimeSource()
        mtimes.touch(0.0)
        wd = self._wd(clock, mtimes)
        clock.advance(300)
        assert wd.poll().tier == "warn"

        # Progress: the transcript is written again.
        clock.advance(1)
        mtimes.touch(clock.now)
        recover_event = wd.poll()
        assert recover_event is not None
        assert recover_event.tier == "ok"
        assert recover_event.recovered is True
        assert wd.current_tier == "ok"
        # Review finding: the RECOVER event must report the length of the stall that just
        # ENDED (baseline 0 → new write at t=301), not ~0s measured from the new write.
        assert recover_event.elapsed == 301

        # A LATER stall must re-fire WARN, not stay silent because "we already warned".
        clock.advance(300)
        second_warn = wd.poll()
        assert second_warn is not None
        assert second_warn.tier == "warn"
        assert second_warn.recovered is False

    def test_preexisting_stale_file_counts_from_watchdog_start_not_old_mtime(self):
        # Review finding: a leftover log from a PREVIOUS run (mtime hours old) made the
        # very first poll read `now - old_mtime`, instantly crossing abort_after and
        # SIGKILLing a freshly started --pid before it wrote anything. A pre-existing
        # old file must get the same measure-from-start grace as a missing one.
        clock, mtimes = FakeClock(10_000.0), FakeMtimeSource()  # started_at = 10000
        mtimes.touch(100.0)  # file exists, last written ages before the watchdog started
        wd = self._wd(clock, mtimes)
        assert wd.poll() is None  # elapsed counts from started_at -> 0, not 9900
        clock.advance(299)
        assert wd.poll() is None
        clock.advance(1)
        event = wd.poll()  # 300s after watchdog start -> a genuine in-watch stall
        assert event is not None
        assert event.tier == "warn"
        assert event.elapsed == 300

    def test_missing_transcript_counts_staleness_from_watchdog_start(self):
        clock, mtimes = FakeClock(100.0), FakeMtimeSource()  # started_at = 100
        mtimes.remove()
        wd = self._wd(clock, mtimes)
        clock.advance(300)  # now = 400, elapsed since started_at(100) = 300
        event = wd.poll()
        assert event is not None
        assert event.tier == "warn"
        assert event.elapsed == 300
        assert event.mtime is None

    def test_slow_poll_interval_does_not_undercount_staleness(self):
        # Progress happens at t=300 but the caller doesn't poll again until t=900 (a slow
        # poll interval). The elapsed-since-progress must be measured from the mtime (300),
        # not from "now minus last poll", so this must already read as ABORT-eligible if
        # abort_after=500, not merely WARN.
        clock, mtimes = FakeClock(0.0), FakeMtimeSource()
        mtimes.touch(0.0)
        wd = self._wd(clock, mtimes, warn_after=100.0, abort_after=500.0)
        clock.advance(50)
        mtimes.touch(50.0)  # progress at t=50, watchdog's baseline should move to 50
        assert wd.poll() is None  # elapsed=0, still ok
        clock.advance(900)  # now = 950, elapsed since baseline(50) = 900 >= abort_after(500)
        event = wd.poll()
        assert event.tier == "abort"
        assert event.elapsed == 900

    def test_rejects_abort_before_warn_threshold(self):
        with pytest.raises(ValueError):
            asw_core.Watchdog(
                "/fake",
                warn_after=1000.0,
                abort_after=10.0,
                clock=FakeClock(),
                get_mtime=FakeMtimeSource(),
            )


# --- actions.build_diagnostics_message --------------------------------------------------


class TestBuildDiagnosticsMessage:
    def _event(self, tier="warn", elapsed=300.0, path="/x/agent-abc.jsonl"):
        return asw_core.Event(tier=tier, elapsed=elapsed, mtime=123.0, transcript_path=path)

    def test_names_transcript_and_inspect_commands(self):
        msg = asw_actions.build_diagnostics_message(self._event())
        assert "/x/agent-abc.jsonl" in msg
        assert "stat -f" in msg
        assert "tail -c 4000 /x/agent-abc.jsonl" in msg

    def test_abort_tier_says_abort(self):
        msg = asw_actions.build_diagnostics_message(self._event(tier="abort", elapsed=1800.0))
        assert "ОСТАНОВКА" in msg
        assert "30.0 мин" in msg

    def test_warn_tier_says_warning(self):
        msg = asw_actions.build_diagnostics_message(self._event(tier="warn"))
        assert "ПРЕДУПРЕЖДЕНИЕ" in msg

    def test_agent_id_adds_sendmessage_taskstop_hint(self):
        msg = asw_actions.build_diagnostics_message(self._event(), agent_id="a1601ca6")
        assert "a1601ca6" in msg
        assert "SendMessage" in msg
        assert "TaskStop" in msg

    def test_no_agent_id_still_gives_a_next_step(self):
        msg = asw_actions.build_diagnostics_message(self._event())
        assert "agent_id не передан" in msg

    def test_description_included_when_given(self):
        msg = asw_actions.build_diagnostics_message(self._event(), description="screenshot recapture")
        assert "screenshot recapture" in msg

    def test_abort_with_pid_says_it_was_killed(self):
        msg = asw_actions.build_diagnostics_message(
            self._event(tier="abort", elapsed=1800.0), pid=4242
        )
        assert "SIGTERM" in msg
        assert "SIGKILL" in msg
        assert "4242" in msg

    def test_abort_without_pid_says_not_stopped(self):
        msg = asw_actions.build_diagnostics_message(self._event(tier="abort", elapsed=1800.0))
        assert "НЕ" in msg  # "процесс НЕ остановлен" — no pid to signal

    def test_is_test_prepends_mandatory_marker_line(self):
        msg = asw_actions.build_diagnostics_message(self._event(), is_test=True)
        first_line = msg.splitlines()[0]
        assert first_line == "ТЕСТ — действий не нужно (тестовый прогон watchdog'а)"

    def test_not_test_has_no_marker_line(self):
        msg = asw_actions.build_diagnostics_message(self._event(), is_test=False)
        assert "ТЕСТ" not in msg

    def test_is_test_with_thresholds_names_the_scaled_down_values_and_prod_default(self):
        # Alex tg#6973: a test alert with no threshold context read as a broken/absurd
        # real policy (21s). Every test alert must say exactly which threshold fired.
        msg = asw_actions.build_diagnostics_message(
            self._event(), is_test=True, warn_after=8.0, abort_after=20.0
        )
        assert "warn=8с" in msg
        assert "abort=20с" in msg
        assert "test-only value" in msg
        assert "5 мин / 30 мин" in msg

    def test_is_test_without_thresholds_omits_the_threshold_line(self):
        msg = asw_actions.build_diagnostics_message(self._event(), is_test=True)
        assert "test-only value" not in msg

    def test_diagnostic_commands_shell_quote_a_hostile_path(self):
        # Review finding: the "Диагностика" lines are commands the reader copy-pastes into
        # a shell; an unquoted path like `/tmp/a; echo OWNED #.log` would execute the
        # embedded `echo OWNED` as a separate command on paste.
        import shlex

        hostile = "/tmp/a; echo OWNED #.log"
        msg = asw_actions.build_diagnostics_message(self._event(path=hostile))
        assert f"tail -c 4000 {hostile}" not in msg
        assert f"tail -c 4000 {shlex.quote(hostile)}" in msg
        assert f'stat -f "%Sm %N" {shlex.quote(hostile)}' in msg


class TestPackageExports:
    def test_public_api_symbols_are_importable_from_the_package_root(self):
        # Review finding: build_nudge_line was in the README's Public API table but not
        # exported from __init__ — keep the table and the package surface in lockstep.
        import agenttools_stall_watchdog as pkg

        for symbol in (
            "Watchdog",
            "Event",
            "classify",
            "mtime_or_none",
            "build_diagnostics_message",
            "build_nudge_line",
            "tmux_nudge",
            "tg_alert",
            "pid_kill",
            "broadcast",
            "TgAlertConfig",
        ):
            assert hasattr(pkg, symbol), f"{symbol} missing from package root"
            assert symbol in pkg.__all__, f"{symbol} missing from __all__"


# --- actions.build_nudge_line -----------------------------------------------------------


class TestBuildNudgeLine:
    def _event(self, tier="warn", elapsed=300.0, path="/x/agent-abc.jsonl"):
        return asw_core.Event(tier=tier, elapsed=elapsed, mtime=1.0, transcript_path=path)

    def test_is_a_single_line(self):
        line = asw_actions.build_nudge_line(self._event(), agent_id="a1", description="d\ne\nf")
        assert "\n" not in line

    def test_names_the_transcript_basename(self):
        line = asw_actions.build_nudge_line(self._event())
        assert "agent-abc.jsonl" in line

    def test_agent_id_and_description_included(self):
        line = asw_actions.build_nudge_line(self._event(), agent_id="a1601ca6", description="recap")
        assert "a1601ca6" in line
        assert "recap" in line

    def test_is_test_prepends_marker(self):
        line = asw_actions.build_nudge_line(self._event(), is_test=True)
        assert line.startswith("TEST, no action needed")

    def test_abort_with_pid_names_the_kill_attempt(self):
        line = asw_actions.build_nudge_line(self._event(tier="abort", elapsed=1800.0), pid=4242)
        assert "4242" in line
        # "attempted", not "killed": pid_kill swallows already-dead/EPERM outcomes, so the
        # message must not overclaim (review finding).
        assert "attempted kill" in line

    def test_is_english_not_russian(self):
        # Alex tg#6967: the tmux nudge is agent-facing and must stay English regardless of
        # the surrounding session's chat language — only the tg alert to Alex is Russian.
        line = asw_actions.build_nudge_line(self._event(tier="abort", elapsed=1800.0), pid=1)
        assert "ABORTED" in line
        assert "убит" not in line
        assert "остановлен" not in line

    def test_is_test_with_thresholds_names_the_scaled_down_values_and_prod_default(self):
        line = asw_actions.build_nudge_line(
            self._event(), is_test=True, warn_after=8.0, abort_after=20.0
        )
        assert "warn=8s" in line
        assert "abort=20s" in line
        assert "prod default 5m/30m" in line

    def test_is_test_without_thresholds_omits_the_threshold_note(self):
        line = asw_actions.build_nudge_line(self._event(), is_test=True)
        assert "prod default" not in line


# --- actions.tmux_nudge -----------------------------------------------------------------


class TestTmuxNudge:
    def test_calls_inject_with_target_and_a_single_line_message(self, monkeypatch):
        calls = []

        def fake_inject(target, text, timeout=5.0):
            calls.append((target, text, timeout))
            return SimpleNamespace(ok=True)

        import agenttools_tmux_inject

        monkeypatch.setattr(agenttools_tmux_inject, "inject", fake_inject)

        action = asw_actions.tmux_nudge("%3")
        event = asw_core.Event(tier="warn", elapsed=300.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)

        assert len(calls) == 1
        target, text, _timeout = calls[0]
        assert target == "%3"
        assert "x.jsonl" in text
        # The multi-line diagnostics block must NEVER reach a live tmux pane — embedded
        # newlines are typed as literal LF bytes and get executed as separate shell
        # commands by the receiving shell (confirmed live: a prior version of this action
        # caused one of its own message lines, `tail -c 4000 <path>`, to actually run in
        # the target pane). Regression test for that incident.
        assert "\n" not in text
        assert "tail -c 4000" not in text

    def test_threads_agent_id_and_description_into_the_nudge(self, monkeypatch):
        calls = []
        import agenttools_tmux_inject

        monkeypatch.setattr(
            agenttools_tmux_inject,
            "inject",
            lambda target, text, timeout=5.0: calls.append(text) or SimpleNamespace(ok=True),
        )
        action = asw_actions.tmux_nudge("%3", agent_id="a1601ca6", description="recap")
        event = asw_core.Event(tier="warn", elapsed=300.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)
        assert "a1601ca6" in calls[0]
        assert "recap" in calls[0]

    def test_never_raises_when_inject_reports_failure(self, monkeypatch):
        import agenttools_tmux_inject

        monkeypatch.setattr(
            agenttools_tmux_inject, "inject", lambda *a, **k: SimpleNamespace(ok=False)
        )
        action = asw_actions.tmux_nudge("%3")
        event = asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x.jsonl")
        action(event)  # must not raise


# --- actions.tg_alert --------------------------------------------------------------------


class TestTgAlert:
    def test_shells_out_to_tg_with_message_and_tag(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        action = asw_actions.tg_alert()
        event = asw_core.Event(tier="warn", elapsed=300.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)

        assert len(calls) == 1
        argv = calls[0]
        assert argv[0] == "tg"
        assert "--tag" in argv and "problem" in argv
        assert any("/x.jsonl" in a for a in argv)

    def test_forwards_tg_agent_flag(self, monkeypatch):
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda argv, **k: calls.append(argv))
        action = asw_actions.tg_alert(asw_actions.TgAlertConfig(agent="claude"))
        event = asw_core.Event(tier="warn", elapsed=300.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)
        argv = calls[0]
        assert "--agent" in argv and "claude" in argv

    def test_binary_missing_does_not_raise(self, monkeypatch):
        def raise_oserror(*a, **k):
            raise FileNotFoundError("no tg on PATH")

        monkeypatch.setattr(subprocess, "run", raise_oserror)
        action = asw_actions.tg_alert()
        event = asw_core.Event(tier="warn", elapsed=300.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)  # must not raise

    def test_timeout_does_not_raise(self, monkeypatch):
        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="tg", timeout=15.0)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        action = asw_actions.tg_alert()
        event = asw_core.Event(tier="abort", elapsed=1800.0, mtime=1.0, transcript_path="/x.jsonl")
        action(event)  # must not raise

    def test_leading_dash_message_is_padded_so_tg_never_parses_it_as_a_flag(self, monkeypatch):
        # Review finding: tg errors on unknown dashed tokens and has no `--` end-of-options
        # marker, so a future template starting with `-` would kill the alert delivery.
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda argv, **k: calls.append(argv))
        monkeypatch.setattr(
            asw_actions, "build_diagnostics_message", lambda *a, **k: "--evil template"
        )
        asw_actions.tg_alert()(
            asw_core.Event(tier="abort", elapsed=1800.0, mtime=1.0, transcript_path="/x")
        )
        message_arg = calls[0][-1]
        assert not message_arg.startswith("-")
        assert message_arg == " --evil template"


# --- actions.pid_kill --------------------------------------------------------------------


class TestPidKill:
    """Every test fully fakes os.getpgid / os.killpg / os.kill — a real signal to a
    hardcoded test pid (or worse, its process group) must never leave the suite.
    """

    def _event(self):
        return asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x.jsonl")

    def test_prefers_the_process_group_and_stops_after_a_clean_sigterm(self, monkeypatch):
        # Review finding: for npm/Playwright/Electron the given pid is a wrapper; killing
        # only it leaves the hung children alive — the group is the whole run.
        import os as os_mod
        import signal as sig_mod

        group_signals = []
        monkeypatch.setattr(os_mod, "getpgid", lambda pid: 999)
        monkeypatch.setattr(os_mod, "killpg", lambda pgid, sig: group_signals.append((pgid, sig)))

        def probe_only(pid, sig):
            assert sig == 0, "plain os.kill must only be the liveness probe when killpg works"
            raise ProcessLookupError()  # dead after SIGTERM

        monkeypatch.setattr(os_mod, "kill", probe_only)
        sleeps = []
        asw_actions.pid_kill(4242, stop_grace=3.0, sleeper=sleeps.append)(self._event())

        assert group_signals == [(999, sig_mod.SIGTERM)]
        assert sleeps == [3.0]

    def test_group_escalates_to_sigkill_if_still_alive_after_grace(self, monkeypatch):
        import os as os_mod
        import signal as sig_mod

        group_signals = []
        monkeypatch.setattr(os_mod, "getpgid", lambda pid: 999)
        monkeypatch.setattr(os_mod, "killpg", lambda pgid, sig: group_signals.append(sig))
        monkeypatch.setattr(os_mod, "kill", lambda pid, sig: None)  # probe: still alive
        sleeps = []
        asw_actions.pid_kill(4242, stop_grace=1.0, sleeper=sleeps.append)(self._event())

        assert group_signals == [sig_mod.SIGTERM, sig_mod.SIGKILL]
        assert sleeps == [1.0]

    def test_falls_back_to_plain_kill_when_the_group_is_unavailable(self, monkeypatch):
        import os as os_mod
        import signal as sig_mod

        def no_group(pid):
            raise ProcessLookupError()

        sent = []

        def fake_kill(pid, sig):
            sent.append(sig)
            if sig == 0:
                return  # still alive

        monkeypatch.setattr(os_mod, "getpgid", no_group)
        monkeypatch.setattr(os_mod, "kill", fake_kill)
        sleeps = []
        asw_actions.pid_kill(4242, stop_grace=1.0, sleeper=sleeps.append)(self._event())

        assert sig_mod.SIGTERM in sent
        assert sig_mod.SIGKILL in sent
        assert sleeps == [1.0]

    def test_already_dead_pid_is_a_noop(self, monkeypatch):
        import os as os_mod

        def no_group(pid):
            raise ProcessLookupError()

        def dead(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os_mod, "getpgid", no_group)
        monkeypatch.setattr(os_mod, "kill", dead)
        sleeps = []
        asw_actions.pid_kill(4242, sleeper=sleeps.append)(self._event())  # must not raise
        assert sleeps == []  # nothing signalled -> no grace wait, no escalation

    def test_never_killpg_the_watchdogs_own_process_group(self, monkeypatch):
        # Review finding: with the documented `nohup watchdog &` launch pattern the
        # watched pid can share the watchdog's OWN process group — a group kill would
        # take down the watchdog and every sibling job. Must fall back to the bare pid.
        import os as os_mod
        import signal as sig_mod

        own_pgrp = os_mod.getpgrp()
        monkeypatch.setattr(os_mod, "getpgid", lambda pid: own_pgrp)
        monkeypatch.setattr(
            os_mod,
            "killpg",
            lambda pgid, sig: (_ for _ in ()).throw(AssertionError("must not killpg own group")),
        )
        sent = []

        def fake_kill(pid, sig):
            sent.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError()  # dead after SIGTERM

        monkeypatch.setattr(os_mod, "kill", fake_kill)
        sleeps = []
        asw_actions.pid_kill(4242, stop_grace=1.0, sleeper=sleeps.append)(self._event())
        assert (4242, sig_mod.SIGTERM) in sent

    def test_nonpositive_pid_is_refused_without_any_signal(self, monkeypatch):
        # Review finding: kill(0)/kill(<negative>) signal a whole GROUP — a pidfile that
        # read `0` must be a refused no-op, never a self-kill of the watchdog's group.
        import os as os_mod

        def forbidden(*a, **k):
            raise AssertionError("no signal API may be touched for pid<=0")

        monkeypatch.setattr(os_mod, "getpgid", forbidden)
        monkeypatch.setattr(os_mod, "killpg", forbidden)
        monkeypatch.setattr(os_mod, "kill", forbidden)
        for bad_pid in (0, -1, -4242):
            asw_actions.pid_kill(bad_pid)(self._event())  # must not raise, must not signal


# --- actions.broadcast --------------------------------------------------------------------


class TestBroadcast:
    def test_runs_every_action(self):
        calls = []
        a1 = lambda e: calls.append("a1")  # noqa: E731
        a2 = lambda e: calls.append("a2")  # noqa: E731
        asw_actions.broadcast(a1, a2)(
            asw_core.Event(tier="warn", elapsed=1.0, mtime=None, transcript_path="/x")
        )
        assert calls == ["a1", "a2"]

    def test_one_raising_does_not_skip_the_rest(self):
        calls = []

        def bad(e):
            raise RuntimeError("boom")

        def good(e):
            calls.append("good")

        asw_actions.broadcast(bad, good)(
            asw_core.Event(tier="warn", elapsed=1.0, mtime=None, transcript_path="/x")
        )
        assert calls == ["good"]


# --- cli -------------------------------------------------------------------------------


class TestCliBuildAction:
    def _args(self, **overrides):
        base = dict(
            dry_run=False,
            tmux_target=None,
            no_tg=True,
            tg_agent=None,
            agent_id=None,
            description=None,
            pid=None,
            test=False,
            tg_on_warn=False,
            warn_after=300.0,
            abort_after=1800.0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_dry_run_yields_no_action(self):
        assert asw_cli._build_action(self._args(dry_run=True)) is None

    def test_no_flags_and_tg_suppressed_yields_no_action(self):
        assert asw_cli._build_action(self._args(no_tg=True)) is None

    def test_tg_enabled_by_default_yields_an_action(self):
        action = asw_cli._build_action(self._args(no_tg=False))
        assert action is not None

    def test_pid_kill_only_fires_on_abort_tier(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            asw_actions, "pid_kill", lambda pid, **k: (lambda e: killed.append(e.tier))
        )
        action = asw_cli._build_action(self._args(no_tg=True, pid=4242))
        assert action is not None
        action(asw_core.Event(tier="warn", elapsed=300.0, mtime=None, transcript_path="/x"))
        assert killed == []
        action(asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x"))
        assert killed == ["abort"]

    def test_tg_alert_default_fires_on_abort_only_not_warn(self, monkeypatch):
        # Alex's tg#6967 correction: the tg (Russian, Alex-facing) channel is abort-tier
        # only by default — Tier-1/warn must reach only the tmux-injected agent nudge.
        fired = []
        monkeypatch.setattr(
            asw_actions, "tg_alert", lambda cfg=None, **k: (lambda e: fired.append(e.tier))
        )
        action = asw_cli._build_action(self._args(no_tg=False))
        assert action is not None
        action(asw_core.Event(tier="warn", elapsed=300.0, mtime=None, transcript_path="/x"))
        assert fired == []
        action(asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x"))
        assert fired == ["abort"]

    def test_tg_on_warn_opts_back_into_warn_tier(self, monkeypatch):
        fired = []
        monkeypatch.setattr(
            asw_actions, "tg_alert", lambda cfg=None, **k: (lambda e: fired.append(e.tier))
        )
        action = asw_cli._build_action(self._args(no_tg=False, tg_on_warn=True))
        assert action is not None
        action(asw_core.Event(tier="warn", elapsed=300.0, mtime=None, transcript_path="/x"))
        assert fired == ["warn"]

    def test_tmux_nudge_fires_on_both_warn_and_abort(self, monkeypatch):
        # Unlike tg, the agent-facing tmux nudge is unchanged by tg#6967: both tiers nudge
        # the agent's own pane.
        nudged = []
        monkeypatch.setattr(
            asw_actions, "tmux_nudge", lambda target, **k: (lambda e: nudged.append(e.tier))
        )
        action = asw_cli._build_action(self._args(no_tg=True, tmux_target="%3"))
        assert action is not None
        action(asw_core.Event(tier="warn", elapsed=300.0, mtime=None, transcript_path="/x"))
        action(asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x"))
        assert nudged == ["warn", "abort"]

    def test_watch_file_flag_is_the_primary_name_transcript_is_an_alias(self):
        parser = asw_cli._build_parser()
        a = parser.parse_args(["watch", "--watch-file", "/x/log.txt"])
        assert a.transcript == "/x/log.txt"
        b = parser.parse_args(["watch", "--transcript", "/y/agent.jsonl"])
        assert b.transcript == "/y/agent.jsonl"


class TestRunWatchOnce:
    """Drives `run_watch` end to end with `--once`, faking `time.time`/`time.sleep` in the
    cli module so the loop body runs with zero real sleeps and a controllable clock.
    """

    def _parser_args(self, tmp_path, **overrides):
        transcript = tmp_path / "agent-x.jsonl"
        transcript.write_text("hello\n")
        parser = asw_cli._build_parser()
        argv = [
            "watch",
            "--transcript",
            str(transcript),
            "--warn-after",
            "300",
            "--abort-after",
            "1800",
            "--once",
            "--no-tg",
        ]
        args = parser.parse_args(argv)
        for k, v in overrides.items():
            setattr(args, k, v)
        return args, transcript

    def test_fresh_transcript_prints_nothing_and_exits_zero(self, tmp_path, capsys):
        args, _t = self._parser_args(tmp_path)
        rc = asw_cli.run_watch(args)
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_stale_transcript_prints_warn_and_exits_zero(self, tmp_path, capsys, monkeypatch):
        args, transcript = self._parser_args(tmp_path)
        # Watchdog starts at `old` (when the file was last written); the poll happens 300s
        # later with no further writes — a genuine in-watch stall.
        import os

        old = 1_000_000.0
        os.utime(transcript, (old, old))
        times = iter([old, old, old + 300])
        monkeypatch.setattr(asw_cli.time, "time", lambda: next(times, old + 300))
        rc = asw_cli.run_watch(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "WARN" in out
        assert str(transcript) in out

    def test_stale_past_abort_exits_two(self, tmp_path, capsys, monkeypatch):
        args, transcript = self._parser_args(tmp_path)
        import os

        old = 1_000_000.0
        os.utime(transcript, (old, old))
        times = iter([old, old, old + 1800])
        monkeypatch.setattr(asw_cli.time, "time", lambda: next(times, old + 1800))
        args.once = False  # the abort branch returns before `--once` is even checked
        rc = asw_cli.run_watch(args)
        out = capsys.readouterr().out
        assert rc == 2
        assert "ABORT" in out

    def test_recovery_prints_recover_line_and_fires_no_actions(self, tmp_path, capsys, monkeypatch):
        # Review finding: the RECOVER branch of run_watch (line format + the "actions do
        # not fire on recovery" guard) was untested. Drive a warn → recover sequence with
        # a scripted clock; a spy action must fire exactly once (on the WARN).
        args, transcript = self._parser_args(tmp_path)
        import os

        old = 1_000_000.0
        os.utime(transcript, (old, old))

        fired = []
        monkeypatch.setattr(asw_cli, "_build_action", lambda a: (lambda e: fired.append(e.tier)))

        # Scripted time: watchdog starts at `old` (mtime of the file), first loop
        # iteration at old+300 (WARN). During its sleep, the transcript is touched
        # (progress); the second iteration at old+320 sees the new mtime and emits
        # RECOVER; run_watch then returns via max_runtime.
        times = iter([old, old + 300, old + 300, old + 300, old + 320, old + 320, old + 400, old + 400])
        monkeypatch.setattr(asw_cli.time, "time", lambda: next(times, old + 400))

        def fake_sleep(_s):
            os.utime(transcript, (old + 310, old + 310))

        monkeypatch.setattr(asw_cli.time, "sleep", fake_sleep)
        args.once = False
        args.max_runtime = 30.0

        rc = asw_cli.run_watch(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "WARN" in out
        assert "RECOVER" in out
        assert "resumed progress" in out
        assert fired == ["warn"]  # nothing fired on the RECOVER crossing

    def test_max_runtime_exits_zero_quietly(self, tmp_path, capsys, monkeypatch):
        # Review finding: the --max-runtime exit path was untested.
        args, transcript = self._parser_args(tmp_path)
        base = 1_000_000.0
        import os

        os.utime(transcript, (base, base))
        times = iter([base, base, base + 10, base + 61, base + 61])
        monkeypatch.setattr(asw_cli.time, "time", lambda: next(times, base + 61))
        monkeypatch.setattr(asw_cli.time, "sleep", lambda s: None)
        args.once = False
        args.max_runtime = 60.0
        rc = asw_cli.run_watch(args)
        assert rc == 0
        assert capsys.readouterr().out == ""


class TestMain:
    def test_keyboard_interrupt_exits_130(self, tmp_path, monkeypatch):
        # Review finding: the KeyboardInterrupt → 130 path in main was untested.
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x\n")

        def raise_interrupt(args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr(asw_cli, "run_watch", raise_interrupt)
        rc = asw_cli.main(["watch", "--watch-file", str(transcript)])
        assert rc == 130

    def test_abort_below_warn_is_a_parser_error_not_a_traceback(self, tmp_path, capsys):
        # Review finding: --abort-after < --warn-after used to escape as Watchdog's raw
        # ValueError traceback; it must be a clean argparse error (SystemExit 2).
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x\n")
        with pytest.raises(SystemExit) as exc_info:
            asw_cli.main(
                ["watch", "--watch-file", str(transcript), "--warn-after", "100", "--abort-after", "10"]
            )
        assert exc_info.value.code == 2
        assert "--abort-after" in capsys.readouterr().err

    def test_nonpositive_pid_is_a_parser_error(self, tmp_path, capsys):
        # Review finding: --pid 0 would killpg the watchdog's own group on abort.
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x\n")
        with pytest.raises(SystemExit) as exc_info:
            asw_cli.main(["watch", "--watch-file", str(transcript), "--pid", "0"])
        assert exc_info.value.code == 2
        assert "--pid" in capsys.readouterr().err


class TestAbortActionOrdering:
    def test_pid_kill_runs_before_the_notifications_on_abort(self, monkeypatch):
        # Review finding: the tmux/tg abort messages describe the kill attempt, so the
        # attempt must have actually run by the time they're sent.
        order = []
        monkeypatch.setattr(
            asw_actions, "pid_kill", lambda pid, **k: (lambda e: order.append("kill"))
        )
        monkeypatch.setattr(
            asw_actions, "tmux_nudge", lambda target, **k: (lambda e: order.append("tmux"))
        )
        monkeypatch.setattr(
            asw_actions, "tg_alert", lambda cfg=None, **k: (lambda e: order.append("tg"))
        )
        parser = asw_cli._build_parser()
        args = parser.parse_args(
            ["watch", "--watch-file", "/x", "--pid", "4242", "--tmux-target", "%3"]
        )
        action = asw_cli._build_action(args)
        action(asw_core.Event(tier="abort", elapsed=1800.0, mtime=None, transcript_path="/x"))
        assert order == ["kill", "tmux", "tg"]
