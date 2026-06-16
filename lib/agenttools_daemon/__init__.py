"""agenttools_daemon — a small, dependency-free process supervisor for the agent-tools ecosystem.

Keep a child process alive across crashes/kills, with exponential backoff between restarts,
a pidfile that survives the supervisor itself, and ``start`` / ``stop`` / ``status`` /
``restart`` / ``run_forever`` controls. This is the one shared copy that ``task-cli`` and
``tg-ctl`` babysit their long-running children with, instead of each hand-rolling a
``while True: spawn; sleep`` loop (slightly differently, and slightly wrong).

What it does
------------
* **Survives crashes/kills** — ``run_forever`` re-spawns the child whenever it exits, until
  ``max_restarts`` is reached or a stop is requested.
* **Exponential backoff between restarts** — ``base * factor ** n`` capped at ``max_delay``
  (a :class:`Backoff`), so a child that crash-loops doesn't hammer the box. Same formula as
  ``agenttools_retry`` but reproduced here, not imported, so this package has zero deps.
* **Pidfile as the source of truth** — ``start`` writes ``{pid, started_at, cmd}`` JSON
  (``0600``, atomic write+rename); ``stop`` reads it to signal the child and removes it;
  ``status`` reads it and liveness-probes the pid, distinguishing ``running`` / ``stale``
  (file present, pid dead) / ``stopped`` (no file).
* **Graceful stop with escalation** — ``stop`` sends ``SIGTERM``, waits ``stop_grace``
  seconds, then escalates to ``SIGKILL`` if the child is still alive.
* **No duplicate starts** — ``start`` refuses if the pidfile already names a live process.
* **Fully injectable seams** — the spawner, clock, sleeper, signaller, and liveness probe
  are all parameters, so the supervisor is testable with a fake process and a fake clock:
  no real long-running children, no real sleeps, deterministic backoff assertions.

Why stdlib only (no ``supervisor``, no ``circus``, no ``python-daemon``)
------------------------------------------------------------------------
The ecosystem is stdlib-first by directive. A focused supervisor is a few hundred lines over
``subprocess`` / ``os`` / ``signal`` / ``time`` and adds zero install/import cost. The
heavyweight process managers (``supervisord``, ``circus``) are daemons-of-daemons with their
own config formats and sockets — far more than ``task-cli`` / ``tg-ctl`` need to keep one
child alive. Owning it also makes every time/process interaction an injected seam, which the
third-party options make impossible to test cleanly.

Quick start
-----------
    from agenttools_daemon import Supervisor, Backoff

    sup = Supervisor(
        ["my-worker", "--serve"],
        pidfile="/tmp/my-worker.pid",
        backoff=Backoff(base=0.5, factor=2.0, max_delay=30.0),
        max_restarts=10,
    )

    sup.start()            # spawn once, write the pidfile (does not supervise)
    sup.status()           # -> Status(state="running", pid=..., ...)
    sup.restart()          # stop + start
    sup.stop()             # SIGTERM, escalate to SIGKILL, remove pidfile

    # Or run the supervision loop in the foreground (e.g. under systemd / a parent):
    sup.run_forever()      # re-spawns on exit with backoff, until cap or stop()

See ``lib/agenttools_daemon/README.md`` for the full reference.
"""

from __future__ import annotations

from .core import (
    AlreadyRunningError,
    Backoff,
    Child,
    Command,
    PidRecord,
    Spawner,
    Status,
    Supervisor,
    default_spawner,
)

__all__ = [
    "AlreadyRunningError",
    "Backoff",
    "Child",
    "Command",
    "PidRecord",
    "Spawner",
    "Status",
    "Supervisor",
    "default_spawner",
]

__version__ = "0.1.0"
