"""agenttools_daemon.core — keep a child process alive across crashes/kills, with backoff.

REACHED AT RUNTIME
    A consumer (``task-cli``, ``tg-ctl``) constructs a :class:`Supervisor` with the command
    to babysit and a pidfile path, then calls one of ``.start()`` / ``.stop()`` /
    ``.status()`` / ``.restart()`` / ``.run_forever()``. ``run_forever`` is the supervision
    loop: it (re)spawns the child whenever it exits, sleeping an exponential backoff between
    restarts, until ``max_restarts`` is hit or a stop is requested.

INVARIANTS
    - **stdlib only at import time.** ``subprocess``, ``os``, ``signal``, ``time``, plus
      ``errno`` / ``json`` / ``pathlib`` / ``dataclasses`` / ``typing``. Nothing third-party.
    - **Everything time- and process-related is injectable.** ``spawner`` makes the child
      (defaults to a real ``subprocess.Popen`` wrapper); ``clock`` and ``sleeper`` are the
      monotonic-now and the sleep. Tests pass fakes, so the suite touches no real process,
      forks nothing, and sleeps zero wall-clock time while still asserting the exact backoff
      schedule.
    - **The pidfile is the cross-process source of truth.** ``start`` writes ``{pid,
      started_at, cmd}`` JSON; ``stop`` reads it to find the child, signals it, and removes
      the file; ``status`` reads it and liveness-probes the pid. A second supervisor pointed
      at the same pidfile refuses to start a duplicate while the recorded pid is alive.
    - **A backoff is self-contained here.** It reuses ``agenttools_retry``'s formula
      (``base * factor ** n`` capped at ``max_delay``) but is NOT imported from it — this
      package must stand alone with zero deps. The shape is intentionally the same so the
      two read identically.

PAST BUGS THIS GUARDS AGAINST
    - A naive supervisor that "spawns in a loop with ``time.sleep``" is untestable: every
      test forks a real process and waits real seconds, so nobody writes the test that
      proves backoff or the restart cap. Here the loop is driven entirely through injected
      seams, so restart-on-exit, the backoff schedule, and the cap are asserted directly.
    - A stale pidfile (process died without cleanup, or pid was recycled by the OS) makes a
      naive ``status`` lie. ``status`` probes liveness with ``os.kill(pid, 0)`` and reports
      ``stale`` (file present, pid dead) distinctly from ``running`` / ``stopped`` so callers
      can clean up instead of trusting the file blindly.
    - ``stop`` that only ``SIGTERM``s and walks away leaves a wedged child. Here ``stop``
      escalates to ``SIGKILL`` after a grace period and only then removes the pidfile.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Union

# A command is the argv list/tuple ``subprocess`` accepts, or a single string (run via the
# shell). We keep it permissive; the spawner decides how to interpret it.
Command = Union[str, Sequence[str]]
PathLike = Union[str, os.PathLike]


class Child(Protocol):
    """The minimal handle a spawner returns — a subset of ``subprocess.Popen``.

    The supervisor only needs the pid, a non-blocking liveness check, and the two signals.
    Keeping the surface this small lets a test pass a trivial fake in place of a real
    process without re-implementing ``Popen``.
    """

    @property
    def pid(self) -> int:  # pragma: no cover - structural
        ...

    def poll(self) -> Optional[int]:
        """Return the exit code if the child has exited, else ``None``. Never blocks."""
        ...

    def terminate(self) -> None:
        """Request a graceful stop (``SIGTERM`` on POSIX)."""
        ...

    def kill(self) -> None:
        """Force a stop (``SIGKILL`` on POSIX)."""
        ...


# A spawner turns a command into a running :class:`Child`. The default wraps
# ``subprocess.Popen``; tests inject a fake that returns a scripted, in-memory child.
Spawner = Callable[[Command], Child]


def default_spawner(cmd: Command) -> Child:
    """Spawn ``cmd`` as a real OS process via ``subprocess.Popen``.

    A string command runs through the shell (``shell=True``); a list/tuple runs directly
    (no shell, the safer default for fixed argv). ``start_new_session=True`` puts the child
    in its own session so a signal to the supervisor isn't implicitly delivered to the whole
    group — the supervisor decides explicitly when the child dies.
    """
    if isinstance(cmd, str):
        return subprocess.Popen(cmd, shell=True, start_new_session=True)  # noqa: S602
    return subprocess.Popen(list(cmd), start_new_session=True)


# ---------------------------------------------------------------------------------------
# Backoff — the same exponential formula agenttools_retry uses, reproduced (not imported)
# so this package stays dependency-free. ``delay(n) = min(base * factor ** n, max_delay)``
# for a 0-based restart index ``n``.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff between restarts. Pure, deterministic, no randomness.

    ``base`` is the first delay; each subsequent restart multiplies by ``factor``, capped at
    ``max_delay``. Index is 0-based: the delay *before* the n-th restart is ``delay(n)``.
    Jitter is intentionally omitted — a single supervised child is not a fleet retrying in
    lockstep, and a deterministic schedule is far easier to reason about and test. (If a
    fleet ever needs spread, wrap the sleeper.)
    """

    base: float = 0.5
    factor: float = 2.0
    max_delay: float = 30.0

    def __post_init__(self) -> None:
        if self.base < 0:
            raise ValueError("base must be >= 0")
        if self.factor < 1:
            raise ValueError("factor must be >= 1")
        if self.max_delay < 0:
            raise ValueError("max_delay must be >= 0")

    def delay(self, restart_index: int) -> float:
        """Seconds to wait before the ``restart_index``-th (0-based) restart."""
        if restart_index < 0:
            raise ValueError("restart_index must be >= 0")
        raw = self.base * (self.factor ** restart_index)
        return min(raw, self.max_delay)


def _coerce_backoff(backoff: Union[Backoff, float, None]) -> Backoff:
    """Accept a ready :class:`Backoff`, a bare number (used as ``base``), or ``None``."""
    if backoff is None:
        return Backoff()
    if isinstance(backoff, Backoff):
        return backoff
    return Backoff(base=float(backoff))


# ---------------------------------------------------------------------------------------
# Pidfile record + status
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PidRecord:
    """What we persist to the pidfile: enough to find and identify the child later."""

    pid: int
    started_at: float  # wall-clock epoch seconds, for humans/status
    cmd: Command

    def to_json(self) -> str:
        payload = {"pid": self.pid, "started_at": self.started_at, "cmd": self.cmd}
        return json.dumps(payload)

    @staticmethod
    def from_json(text: str) -> "PidRecord":
        data = json.loads(text)
        return PidRecord(
            pid=int(data["pid"]),
            started_at=float(data.get("started_at", 0.0)),
            cmd=data.get("cmd", ""),
        )


@dataclass(frozen=True)
class Status:
    """A snapshot of the supervised process as seen through the pidfile + a liveness probe.

    ``state`` is one of:
      - ``"running"``  — pidfile present and the recorded pid is alive.
      - ``"stale"``    — pidfile present but the recorded pid is dead (crashed/killed without
                          cleanup, or the pid was recycled). The caller should clean up.
      - ``"stopped"``  — no pidfile (nothing is supposed to be running).
    """

    state: str  # "running" | "stale" | "stopped"
    pid: Optional[int] = None
    started_at: Optional[float] = None
    cmd: Optional[Command] = None

    @property
    def running(self) -> bool:
        return self.state == "running"


# ---------------------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------------------


class AlreadyRunningError(RuntimeError):
    """``start()`` was called while a live process is already recorded in the pidfile."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"a supervised process is already running (pid {pid})")
        self.pid = pid


@dataclass
class Supervisor:
    """Keep a child process alive across crashes/kills, with exponential backoff.

    Construct with the command to babysit and a pidfile path; everything else has a safe
    default. The time/process seams (``spawner`` / ``clock`` / ``sleeper`` / ``signaller``
    / ``alive``) are injectable so the whole thing is testable without a real process.

    Parameters
    ----------
    cmd
        The command to run — an argv list/tuple (run directly) or a string (run via shell).
    pidfile
        Path to the JSON pidfile. Written on ``start``, read by ``status`` / ``stop``,
        removed on ``stop``.
    backoff
        A :class:`Backoff`, or a bare number used as its ``base``. Controls the sleep between
        restarts in ``run_forever``.
    max_restarts
        Cap on **restarts** in a single ``run_forever`` call (``None`` = unlimited). The
        first spawn is not a restart; ``max_restarts=0`` means "run once, never restart".
    stop_grace
        Seconds to wait after ``SIGTERM`` before escalating to ``SIGKILL`` in ``stop``.
    spawner / clock / sleeper / signaller / alive
        Injected seams (see field docs). Defaults use the real OS.
    """

    cmd: Command
    pidfile: PathLike
    backoff: Union[Backoff, float, None] = None
    max_restarts: Optional[int] = None
    stop_grace: float = 5.0

    # --- injectable seams (real OS by default) ---
    spawner: Spawner = default_spawner
    # monotonic-ish "now" in seconds; only used for relative waits (stop grace polling).
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    # send a signal to an arbitrary pid (cross-process stop path). Defaults to os.kill.
    signaller: Callable[[int, int], None] = os.kill
    # liveness probe for an arbitrary pid (used by status/stop across processes).
    alive: Callable[[int], bool] = field(default=None)  # type: ignore[assignment]

    # --- internal state ---
    _backoff: Backoff = field(init=False, repr=False)
    _child: Optional[Child] = field(default=None, init=False, repr=False)
    _stop_requested: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._backoff = _coerce_backoff(self.backoff)
        if self.max_restarts is not None and self.max_restarts < 0:
            raise ValueError("max_restarts must be >= 0 or None")
        if self.stop_grace < 0:
            raise ValueError("stop_grace must be >= 0")
        if self.alive is None:
            self.alive = _pid_alive

    # --- pidfile helpers ---------------------------------------------------------------

    @property
    def _pidpath(self) -> Path:
        return Path(self.pidfile)

    def _read_record(self) -> Optional[PidRecord]:
        try:
            text = self._pidpath.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        text = text.strip()
        if not text:
            return None
        try:
            return PidRecord.from_json(text)
        except (ValueError, KeyError, TypeError):
            # A corrupt pidfile is treated as "no record" rather than crashing the
            # supervisor; status will report ``stopped`` and start can overwrite it.
            return None

    def _write_record(self, record: PidRecord) -> None:
        path = self._pidpath
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write+rename so a reader never sees a half-written file. 0600: a pidfile leaks the
        # command line; keep it owner-only, matching the ecosystem's privacy posture.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(record.to_json())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _remove_pidfile(self) -> None:
        try:
            self._pidpath.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # --- public API --------------------------------------------------------------------

    def start(self) -> PidRecord:
        """Spawn the child once and record it in the pidfile. Does NOT supervise.

        Refuses (raises :class:`AlreadyRunningError`) if the pidfile already names a live
        process — two supervisors must not babysit the same slot. A stale pidfile (recorded
        pid is dead) is silently reclaimed.
        """
        record = self._read_record()
        if record is not None and self.alive(record.pid):
            raise AlreadyRunningError(record.pid)
        child = self.spawner(self.cmd)
        self._child = child
        rec = PidRecord(pid=child.pid, started_at=time.time(), cmd=self.cmd)
        self._write_record(rec)
        return rec

    def stop(self) -> bool:
        """Stop the supervised child and remove the pidfile. Idempotent.

        Sets the stop flag (so a concurrent ``run_forever`` exits), then signals the pid from
        the pidfile: ``SIGTERM`` first, and ``SIGKILL`` if it is still alive after
        ``stop_grace`` seconds. Returns ``True`` if a live process was signalled, ``False`` if
        nothing was running. Always clears the pidfile.
        """
        self._stop_requested = True
        record = self._read_record()
        if record is None:
            self._remove_pidfile()
            return False

        pid = record.pid
        if not self.alive(pid):
            # Stale pidfile — nothing to kill, just clean up.
            self._remove_pidfile()
            return False

        self._signal(pid, signal.SIGTERM)
        if not self._wait_until_dead(pid, self.stop_grace):
            self._signal(pid, signal.SIGKILL)
            # Give the kernel a final moment; we don't loop forever on an unkillable pid.
            self._wait_until_dead(pid, self.stop_grace)

        self._remove_pidfile()
        self._child = None
        return True

    def status(self) -> Status:
        """Report the supervised process's state from the pidfile + a liveness probe."""
        record = self._read_record()
        if record is None:
            return Status(state="stopped")
        if self.alive(record.pid):
            return Status(
                state="running",
                pid=record.pid,
                started_at=record.started_at,
                cmd=record.cmd,
            )
        return Status(
            state="stale",
            pid=record.pid,
            started_at=record.started_at,
            cmd=record.cmd,
        )

    def restart(self) -> PidRecord:
        """Stop any running child, then start a fresh one. Returns the new record."""
        self.stop()
        # ``stop`` set the stop flag for any run_forever; clear it so a manual restart leaves
        # the supervisor usable again.
        self._stop_requested = False
        return self.start()

    def run_forever(self) -> int:
        """Supervise: spawn, and re-spawn on every exit with backoff, until the cap or stop.

        Returns the number of restarts performed (0 if the first child ran until a stop was
        requested, or if ``max_restarts=0`` and the child exited once). Honors a
        ``stop()`` requested from another thread/signal handler between iterations.

        The loop:
          1. spawn (or reuse a child from a prior ``start``) and record it,
          2. wait for it to exit (polling via the injected clock/sleeper),
          3. if a stop was requested, return,
          4. otherwise, if restarts remain, sleep ``backoff.delay(n)`` and respawn,
          5. when restarts are exhausted, return.
        """
        self._stop_requested = False
        restarts = 0

        # First child: reuse one a prior ``start`` left attached, else spawn fresh.
        child = self._child
        if child is None or child.poll() is not None:
            child = self._spawn_and_record()

        while True:
            self._await_exit(child)
            self._remove_pidfile()
            self._child = None

            if self._stop_requested:
                return restarts
            if self.max_restarts is not None and restarts >= self.max_restarts:
                return restarts

            self.sleeper(self._backoff.delay(restarts))
            if self._stop_requested:  # a stop landed during the backoff sleep
                return restarts

            restarts += 1
            child = self._spawn_and_record()

    # --- internals ---------------------------------------------------------------------

    def _spawn_and_record(self) -> Child:
        child = self.spawner(self.cmd)
        self._child = child
        self._write_record(
            PidRecord(pid=child.pid, started_at=time.time(), cmd=self.cmd)
        )
        return child

    def _await_exit(self, child: Child) -> Optional[int]:
        """Block (via the injected sleeper) until the child exits; return its exit code.

        Polls ``child.poll()`` rather than ``wait()`` so the loop checks ``_stop_requested``
        and the fake clock advances deterministically in tests. A real implementation could
        ``wait()`` directly; polling keeps the seam uniform and stop-responsive.
        """
        while True:
            code = child.poll()
            if code is not None:
                return code
            if self._stop_requested:
                return None
            self.sleeper(_POLL_INTERVAL)

    def _wait_until_dead(self, pid: int, grace: float) -> bool:
        """Poll until ``pid`` is dead or ``grace`` seconds elapse. ``True`` if it died."""
        deadline = self.clock() + grace
        while True:
            if not self.alive(pid):
                return True
            if self.clock() >= deadline:
                return False
            self.sleeper(min(_POLL_INTERVAL, grace) if grace > 0 else 0.0)

    def _signal(self, pid: int, sig: int) -> None:
        """Best-effort signal: a race where the pid already exited is not an error."""
        try:
            self.signaller(pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise


# A single shared poll interval for the wait loops. Small enough to be responsive, and in
# tests the injected sleeper makes it cost nothing — what matters is the call count, not the
# value. Kept module-level so both wait loops agree.
_POLL_INTERVAL = 0.05


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process this caller can signal? (``os.kill(pid, 0)`` probe.)

    ``ESRCH`` means no such process (dead). ``EPERM`` means the process exists but is owned
    by someone else — still "alive" for our purposes. Any other error is treated as dead to
    stay on the safe side rather than wedge the supervisor.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


__all__ = [
    "Supervisor",
    "Backoff",
    "Status",
    "PidRecord",
    "AlreadyRunningError",
    "default_spawner",
    "Command",
    "Child",
    "Spawner",
]
