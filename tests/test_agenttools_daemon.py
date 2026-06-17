"""Tests for agenttools_daemon — the shared, dependency-free process supervisor.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_daemon.py -q
    # or, if agenttools-daemon is installed:  python -m pytest tests/ -q

Every test injects a fake spawner and a fake clock/sleeper, so the suite touches no real
process, forks nothing, and sleeps zero wall-clock time while asserting the exact backoff
schedule and restart behaviour. The pidfile lives under pytest's ``tmp_path`` (HOME-isolated),
so nothing leaks between tests or onto the developer's machine.
"""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_daemon as atd  # noqa: E402
from agenttools_daemon import (  # noqa: E402
    AlreadyRunningError,
    Backoff,
    PidRecord,
    Status,
    Supervisor,
)


# --- fakes ------------------------------------------------------------------------------


class FakeChild:
    """An in-memory stand-in for a ``subprocess.Popen``.

    ``exit_after`` controls liveness: ``poll()`` returns ``None`` for the first
    ``exit_after`` calls (still running), then the scripted ``exit_code`` (exited). With
    ``exit_after=0`` the child is "already exited" on the very first poll — the common shape
    for "the child crashed immediately" tests. Records ``terminated`` / ``killed`` so a stop
    test can assert escalation.
    """

    _next_pid = 1000

    def __init__(self, exit_after: int = 0, exit_code: int = 0) -> None:
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self._polls_left = exit_after
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakeSpawner:
    """A spawner that hands out scripted :class:`FakeChild` objects and counts spawns.

    ``children`` is a list of (exit_after, exit_code) scripts consumed in order; once the
    list runs out, every further spawn produces an immediately-exiting child. ``spawned``
    accumulates the children it produced so a test can inspect them.
    """

    def __init__(self, children=None) -> None:
        self._scripts = list(children or [])
        self.calls = 0
        self.spawned: list[FakeChild] = []

    def __call__(self, cmd):
        self.calls += 1
        if self._scripts:
            exit_after, exit_code = self._scripts.pop(0)
        else:
            exit_after, exit_code = (0, 0)
        child = FakeChild(exit_after=exit_after, exit_code=exit_code)
        self.spawned.append(child)
        return child


class Recorder:
    """A fake sleeper: records every delay it is asked to sleep, sleeps zero real time."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakeClock:
    """A monotonic clock under test control: advances only when ``advance`` is called."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make(tmp_path, **kwargs):
    """Build a Supervisor wired to fakes, with a pidfile under ``tmp_path``."""
    pidfile = tmp_path / "child.pid"
    spawner = kwargs.pop("spawner", FakeSpawner())
    sleeper = kwargs.pop("sleeper", Recorder())
    clock = kwargs.pop("clock", FakeClock())
    # Default: nothing is alive unless a test says so (no real pids involved).
    alive = kwargs.pop("alive", lambda pid: False)
    sup = Supervisor(
        kwargs.pop("cmd", ["worker", "--serve"]),
        pidfile=pidfile,
        spawner=spawner,
        sleeper=sleeper,
        clock=clock,
        alive=alive,
        **kwargs,
    )
    return sup, spawner, sleeper, clock, pidfile


# --- Backoff ----------------------------------------------------------------------------


def test_backoff_schedule_is_exponential_and_capped():
    b = Backoff(base=0.5, factor=2.0, max_delay=4.0)
    assert [b.delay(n) for n in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]


def test_backoff_factor_one_is_constant():
    b = Backoff(base=1.5, factor=1.0, max_delay=30.0)
    assert [b.delay(n) for n in range(4)] == [1.5, 1.5, 1.5, 1.5]


def test_backoff_rejects_bad_params():
    with pytest.raises(ValueError):
        Backoff(base=-1.0)
    with pytest.raises(ValueError):
        Backoff(factor=0.5)
    with pytest.raises(ValueError):
        Backoff(max_delay=-1.0)
    with pytest.raises(ValueError):
        Backoff().delay(-1)


def test_backoff_coercion_from_bare_number(tmp_path):
    sup, *_ = _make(tmp_path, backoff=0.25)
    # A bare number sets the base; defaults keep factor=2.0.
    assert sup._backoff.delay(0) == 0.25
    assert sup._backoff.delay(1) == 0.5


# --- start / pidfile lifecycle ----------------------------------------------------------


def test_start_spawns_once_and_writes_pidfile(tmp_path):
    sup, spawner, _sleeper, _clock, pidfile = _make(tmp_path)
    record = sup.start()

    assert spawner.calls == 1
    assert isinstance(record, PidRecord)
    assert record.pid == spawner.spawned[0].pid
    assert pidfile.exists()

    data = json.loads(pidfile.read_text())
    assert data["pid"] == record.pid
    assert data["cmd"] == ["worker", "--serve"]
    assert "started_at" in data


def test_pidfile_is_owner_only(tmp_path):
    sup, _spawner, _sleeper, _clock, pidfile = _make(tmp_path)
    sup.start()
    mode = pidfile.stat().st_mode & 0o777
    assert mode == 0o600


def test_start_refuses_when_a_live_process_is_already_recorded(tmp_path):
    # Pre-seed a pidfile naming a pid the supervisor will see as alive.
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=7777, started_at=1.0, cmd=["x"]).to_json())
    sup, spawner, *_ = _make(tmp_path, alive=lambda pid: pid == 7777)

    with pytest.raises(AlreadyRunningError) as ei:
        sup.start()
    assert ei.value.pid == 7777
    assert spawner.calls == 0  # never spawned a duplicate


def test_start_reclaims_a_stale_pidfile(tmp_path):
    # Pidfile names a dead pid (alive() is False for everything) — start should overwrite it.
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=999999, started_at=1.0, cmd=["x"]).to_json())
    sup, spawner, *_ = _make(tmp_path)

    record = sup.start()
    assert spawner.calls == 1
    assert record.pid != 999999
    assert json.loads(pidfile.read_text())["pid"] == record.pid


def test_corrupt_pidfile_is_treated_as_no_record(tmp_path):
    pidfile = tmp_path / "child.pid"
    pidfile.write_text("{ this is not json")
    sup, spawner, *_ = _make(tmp_path)

    assert sup.status().state == "stopped"
    sup.start()  # overwrites the garbage rather than crashing
    assert spawner.calls == 1


# --- status -----------------------------------------------------------------------------


def test_status_stopped_when_no_pidfile(tmp_path):
    sup, *_ = _make(tmp_path)
    st = sup.status()
    assert isinstance(st, Status)
    assert st.state == "stopped"
    assert st.running is False
    assert st.pid is None


def test_status_running_when_pid_is_alive(tmp_path):
    sup, spawner, _sleeper, _clock, _pidfile = _make(tmp_path)
    record = sup.start()
    # Now claim the spawned pid is alive.
    sup.alive = lambda pid: pid == record.pid

    st = sup.status()
    assert st.state == "running"
    assert st.running is True
    assert st.pid == record.pid
    assert st.cmd == ["worker", "--serve"]


def test_status_stale_when_pidfile_present_but_pid_dead(tmp_path):
    sup, _spawner, _sleeper, _clock, _pidfile = _make(tmp_path)
    sup.start()  # alive() defaults to False, so the recorded pid reads as dead
    st = sup.status()
    assert st.state == "stale"
    assert st.running is False
    assert st.pid is not None


# --- run_forever: restart-on-exit + backoff + cap ---------------------------------------


def test_run_forever_restarts_on_exit_with_backoff_until_cap(tmp_path):
    # Every child exits immediately (exit_after=0). With max_restarts=3 we expect the
    # initial spawn + 3 restarts = 4 spawns, and 3 backoff sleeps on the default schedule.
    sup, spawner, sleeper, _clock, pidfile = _make(
        tmp_path, max_restarts=3, backoff=Backoff(base=0.5, factor=2.0, max_delay=30.0)
    )

    restarts = sup.run_forever()

    assert restarts == 3
    assert spawner.calls == 4  # 1 initial + 3 restarts
    assert sleeper.delays == [0.5, 1.0, 2.0]  # exact backoff schedule, no jitter
    assert not pidfile.exists()  # loop cleans up the pidfile when it returns


def test_run_forever_zero_restarts_runs_once(tmp_path):
    sup, spawner, sleeper, _clock, _pidfile = _make(tmp_path, max_restarts=0)
    restarts = sup.run_forever()
    assert restarts == 0
    assert spawner.calls == 1  # ran exactly once, never restarted
    assert sleeper.delays == []  # no backoff because there was no restart


def test_run_forever_backoff_is_capped_at_max_delay(tmp_path):
    sup, _spawner, sleeper, _clock, _pidfile = _make(
        tmp_path, max_restarts=5, backoff=Backoff(base=1.0, factor=2.0, max_delay=4.0)
    )
    sup.run_forever()
    # delay(0..4) = 1, 2, 4, 4, 4 (capped).
    assert sleeper.delays == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_run_forever_adopts_a_child_left_by_start(tmp_path):
    # A child that survives a few polls then exits; start() attaches it, run_forever adopts it.
    spawner = FakeSpawner(children=[(3, 0)])  # first child polls None x3 then exits
    sup, _spawner, _sleeper, _clock, _pidfile = _make(
        tmp_path, spawner=spawner, max_restarts=0
    )
    sup.start()
    assert spawner.calls == 1
    restarts = sup.run_forever()
    assert restarts == 0
    # run_forever adopted the already-spawned child instead of spawning a second one.
    assert spawner.calls == 1


def test_run_forever_unlimited_restarts_stops_on_request(tmp_path):
    # max_restarts=None (unlimited). A custom sleeper requests stop after 2 backoff sleeps,
    # proving the loop honours a stop landing during the backoff window and doesn't run away.
    holder = {}

    class StoppingSleeper(Recorder):
        def __call__(self, delay):
            super().__call__(delay)
            if len(self.delays) >= 2:
                holder["sup"].stop()

    stopping = StoppingSleeper()
    sup, spawner, _sleeper, _clock, _pidfile = _make(
        tmp_path, max_restarts=None, sleeper=stopping
    )
    holder["sup"] = sup

    restarts = sup.run_forever()
    # Schedule: spawn#1 exits -> backoff 0.5 -> restart#1 spawn#2 exits -> backoff 1.0, and
    # the stop lands DURING that 2nd backoff sleep, so the loop returns before spawning again.
    # One restart completed (spawn#2); the would-be restart#2 was cancelled by the stop.
    assert stopping.delays == [0.5, 1.0]
    assert restarts == 1
    assert spawner.calls == 2  # initial + 1 completed restart, no spawn after the stop


# --- stop -------------------------------------------------------------------------------


def test_stop_when_nothing_running_returns_false(tmp_path):
    sup, *_ = _make(tmp_path)
    assert sup.stop() is False


def test_stop_sends_sigterm_to_a_live_pid_and_removes_pidfile(tmp_path):
    sent = []
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=["x"]).to_json())

    # Alive until SIGTERM is delivered, then dead — so no SIGKILL escalation is needed.
    state = {"alive": True}

    def signaller(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGTERM:
            state["alive"] = False

    sup, *_ = _make(
        tmp_path,
        signaller=signaller,
        alive=lambda pid: state["alive"] and pid == 4242,
    )

    assert sup.stop() is True
    assert sent == [(4242, signal.SIGTERM)]  # graceful term was enough
    assert not pidfile.exists()


def test_stop_escalates_to_sigkill_when_term_is_ignored(tmp_path):
    sent = []
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=["x"]).to_json())

    state = {"alive": True}
    clock = FakeClock()

    def signaller(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            state["alive"] = False  # only SIGKILL actually ends it

    # The grace-wait sleeper advances the fake clock so the deadline is reached and the
    # supervisor escalates — no real time passes.
    def advancing_sleep(dt):
        clock.advance(max(dt, 1.0))

    sup, *_ = _make(
        tmp_path,
        signaller=signaller,
        clock=clock,
        sleeper=advancing_sleep,
        stop_grace=2.0,
        alive=lambda pid: state["alive"] and pid == 4242,
    )

    assert sup.stop() is True
    assert (4242, signal.SIGTERM) in sent
    assert (4242, signal.SIGKILL) in sent  # escalated after the grace window
    assert not pidfile.exists()


def test_stop_on_stale_pidfile_cleans_up_without_signalling(tmp_path):
    sent = []
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=["x"]).to_json())
    sup, *_ = _make(
        tmp_path,
        signaller=lambda pid, sig: sent.append((pid, sig)),
        alive=lambda pid: False,  # recorded pid is already dead
    )

    assert sup.stop() is False  # nothing live to kill
    assert sent == []  # never signalled a dead pid
    assert not pidfile.exists()  # but cleaned up the stale file


def test_stop_swallows_process_lookup_race(tmp_path):
    # The pid exits between the liveness probe and the signal: ProcessLookupError must not
    # escape stop().
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=["x"]).to_json())

    # The process is alive at the first probe, then exits right before we signal it: the
    # signal raises ProcessLookupError and the pid reads dead from then on (the real race).
    state = {"probed": False}

    def racy_alive(pid):
        if not state["probed"]:
            state["probed"] = True
            return True  # looked alive when stop() first probed
        return False  # gone by the time the grace-wait re-checks

    def racy_signaller(pid, sig):
        raise ProcessLookupError("gone")

    sup, *_ = _make(
        tmp_path,
        signaller=racy_signaller,
        alive=racy_alive,
    )
    # Should not raise (ProcessLookupError is swallowed) and the file is still cleaned up.
    sup.stop()
    assert not pidfile.exists()


# --- restart ----------------------------------------------------------------------------


def test_restart_stops_then_starts_a_fresh_child(tmp_path):
    pidfile = tmp_path / "child.pid"
    pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=["x"]).to_json())

    state = {"alive": True}

    def signaller(pid, sig):
        state["alive"] = False

    spawner = FakeSpawner()
    sup, _spawner, _sleeper, _clock, _pidfile = _make(
        tmp_path,
        spawner=spawner,
        signaller=signaller,
        alive=lambda pid: state["alive"] and pid == 4242,
    )

    record = sup.restart()
    assert spawner.calls == 1  # one fresh spawn after the stop
    assert record.pid != 4242
    assert pidfile.exists()
    assert json.loads(pidfile.read_text())["pid"] == record.pid


def test_restart_clears_stop_flag_for_a_later_run_forever(tmp_path):
    # restart() internally calls stop() (which sets the stop flag); it must clear it so a
    # subsequent run_forever isn't an immediate no-op.
    sup, spawner, _sleeper, _clock, _pidfile = _make(tmp_path, max_restarts=1)
    sup.restart()
    spawner_calls_after_restart = spawner.calls
    sup.run_forever()
    # run_forever did real work (spawned at least once more), proving the flag was cleared.
    assert spawner.calls > spawner_calls_after_restart


# --- PidRecord round-trip ---------------------------------------------------------------


def test_pidrecord_json_round_trip():
    rec = PidRecord(pid=123, started_at=456.5, cmd=["a", "b"])
    again = PidRecord.from_json(rec.to_json())
    assert again == rec


def test_module_exports_public_surface():
    # Guard the FULL public surface the README documents and __all__ promises — not just a
    # subset — so dropping/renaming an export (Child / Spawner / Command / default_spawner
    # included) trips a test rather than silently breaking a consumer's import.
    expected = {
        "AlreadyRunningError",
        "Backoff",
        "Child",
        "Command",
        "PidRecord",
        "Spawner",
        "Status",
        "Supervisor",
        "default_spawner",
    }
    for name in expected:
        assert hasattr(atd, name), name
    assert expected <= set(atd.__all__)
    assert atd.__version__
