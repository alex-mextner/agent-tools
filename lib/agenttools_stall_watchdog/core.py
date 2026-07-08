"""Core stall-detection logic for :mod:`agenttools_stall_watchdog`.

The public surface (``Watchdog``, ``Event``, ``Tier``, ``classify``) is re-exported from
the package ``__init__``; import from there, not from this module.

What this does
---------------
Watches ONE transcript file's mtime (a background Claude Code subagent's
``subagents/agent-<id>.jsonl``, per the ecosystem's established liveness signal — mtime
advancing is progress, per ``feedback-sendmessage-queued-not-proof-of-liveness``) and
classifies elapsed no-progress time into three tiers:

* ``ok``    — mtime advanced within ``warn_after`` seconds.
* ``warn``  — stale for >= ``warn_after`` but < ``abort_after`` seconds.
* ``abort`` — stale for >= ``abort_after`` seconds.

Design notes
------------
* **Pure classification, no I/O.** :func:`classify` is a bare function of two numbers; the
  disk read (``os.stat``) is a caller-supplied seam (``get_mtime``), same as
  ``agenttools_daemon``'s injectable clock/spawner. The whole module is testable with a
  fake clock and a fake mtime source — no real files, no real sleeps.
* **Edge-triggered, not level-triggered.** :meth:`Watchdog.poll` returns an
  :class:`Event` only on a THRESHOLD CROSSING (entering ``warn``, entering ``abort``, or
  recovering back to ``ok`` after either), never once per tick while a tier is merely
  still active. A caller polling every few seconds must not re-fire the same warning on
  every tick, and must not stay silent forever once a stall recovers.
* **Recovery resets the episode.** If the transcript starts advancing again after a WARN
  (mtime > the mtime last observed when WARN fired), the watchdog treats that as a fresh
  episode: a later stall re-fires WARN/ABORT rather than staying silent because "we
  already warned once." This is acceptance criterion #3 (see the ticket).
* **Missing transcript is staleness from watchdog-start, not an error.** A subagent that
  hasn't written its first transcript line yet (very first tool call still in flight) is
  indistinguishable from "no progress" — ``get_mtime`` returning ``None`` is treated as
  "no mtime observed yet," and elapsed time is measured from when the :class:`Watchdog`
  was constructed (``started_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

Tier = Literal["ok", "warn", "abort"]

GetMtime = Callable[[str], Optional[float]]
Clock = Callable[[], float]


def classify(elapsed: float, warn_after: float, abort_after: float) -> Tier:
    """Classify ``elapsed`` seconds of no-progress into a :data:`Tier`.

    Pure — no clock reads, no I/O. ``abort_after`` is checked first so a config with
    ``abort_after <= warn_after`` (a misconfiguration, but not this function's job to
    reject) still resolves to the more severe tier rather than getting stuck at "warn".
    """
    if elapsed >= abort_after:
        return "abort"
    if elapsed >= warn_after:
        return "warn"
    return "ok"


@dataclass(frozen=True)
class Event:
    """One threshold crossing, as returned by :meth:`Watchdog.poll` (``None`` = no crossing).

    ``tier`` is the tier just entered (``"warn"``/``"abort"``) or ``"ok"`` when this event
    is a RECOVERY (progress resumed after a warn/abort). ``elapsed`` is the no-progress
    duration in seconds at the moment of the crossing; ``mtime`` is the last-observed
    transcript mtime (``None`` if the transcript never appeared).
    """

    tier: Tier
    elapsed: float
    mtime: Optional[float]
    transcript_path: str
    recovered: bool = False


class Watchdog:
    """Stateful poller for one transcript path. Call :meth:`poll` on a timer; it returns
    an :class:`Event` only on a threshold crossing, else ``None``.

    All I/O is via the injected ``clock`` and ``get_mtime`` seams — no module-level
    ``time.time()`` / ``os.stat`` call, so tests drive it with fakes and zero real time.
    """

    def __init__(
        self,
        transcript_path: str,
        *,
        warn_after: float = 300.0,
        abort_after: float = 1800.0,
        clock: Clock,
        get_mtime: GetMtime,
    ) -> None:
        if abort_after < warn_after:
            raise ValueError(
                f"abort_after ({abort_after}) must be >= warn_after ({warn_after})"
            )
        self.transcript_path = transcript_path
        self.warn_after = warn_after
        self.abort_after = abort_after
        self._clock = clock
        self._get_mtime = get_mtime
        self.started_at = clock()
        # The mtime the episode's progress clock is measured from — either the real
        # transcript mtime once observed, or `started_at` while the file doesn't exist yet.
        self._baseline: float = self.started_at
        self._last_seen_mtime: Optional[float] = None
        self._current_tier: Tier = "ok"

    def _observe(self) -> "tuple[float, float]":
        """Read the transcript mtime, advance ``_baseline`` on new progress.

        Returns ``(elapsed, stalled_for)``: ``elapsed`` is the CURRENT no-progress time
        (post-reset — what tier classification uses), ``stalled_for`` is how long the
        PREVIOUS episode had been stale at the moment progress was observed (pre-reset).
        The two differ only on a progress tick; keeping both fixes the review finding
        that a RECOVER event used to report ~0s (the time since the NEW write) instead of
        the actual stall duration.
        """
        now = self._clock()
        mtime = self._get_mtime(self.transcript_path)
        stalled_for = now - self._baseline
        if mtime is not None and mtime != self._last_seen_mtime:
            # Progress: the file was written since we last looked. The previous episode's
            # stall length is measured up to THAT write, not up to `now` — a slow poll
            # interval must not inflate it. Then reset the no-progress clock to the write
            # (not to `now`), so staleness accrued since the poll still counts.
            #
            # Clamp to `started_at`: a PRE-EXISTING file with an old mtime (a previous
            # run's leftover log, or a fresh process that hasn't written its first line
            # yet) must get the same measure-from-watchdog-start grace as a missing file —
            # otherwise the very first poll reads `now - old_mtime`, instantly crosses
            # abort_after, and SIGKILLs a freshly started --pid before it ever wrote
            # anything (review finding). A live write during the watch has
            # mtime >= started_at, so the clamp never distorts real progress.
            stalled_for = max(0.0, mtime - self._baseline)
            self._last_seen_mtime = mtime
            self._baseline = max(mtime, self.started_at)
        return now - self._baseline, stalled_for

    def poll(self) -> Optional[Event]:
        """Advance one tick. Returns an :class:`Event` on a tier change, else ``None``."""
        elapsed, stalled_for = self._observe()
        tier = classify(elapsed, self.warn_after, self.abort_after)
        if tier == self._current_tier:
            return None
        recovered = tier == "ok" and self._current_tier != "ok"
        event = Event(
            tier=tier,
            # A recovery event reports the length of the stall that just ENDED — the
            # only duration that means anything on a RECOVER line ("resumed after Ns
            # stale"); a warn/abort crossing reports the current no-progress time.
            elapsed=stalled_for if recovered else elapsed,
            mtime=self._last_seen_mtime,
            transcript_path=self.transcript_path,
            recovered=recovered,
        )
        self._current_tier = tier
        return event

    @property
    def current_tier(self) -> Tier:
        return self._current_tier


def mtime_or_none(path: str) -> Optional[float]:
    """The real ``get_mtime`` implementation: ``os.stat(path).st_mtime``, or ``None``.

    Deferred ``os`` import keeps the module importable with zero cost (ecosystem's
    lazy-heavy-imports convention); ``os`` is effectively free but the pattern is kept
    consistent with the rest of the ecosystem's lib modules.
    """
    import os

    try:
        return os.stat(path).st_mtime
    except OSError:
        return None
