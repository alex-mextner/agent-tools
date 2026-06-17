"""agenttools_tmux_inject — inject text/keys into a named tmux pane or session.

A finishing agent posts a line into *another* agent's interactive pane via tmux's
``send-keys``: e.g. task-cli, on completing a task, types "done, X unblocked" into the
pane of the agent that was blocked on it. stdlib-only (``subprocess`` + ``shutil``), with
the heavy imports deferred into the call path so importing this package stays free.

Quick start
-----------
    from agenttools_tmux_inject import inject, has_session

    if has_session("work"):
        result = inject("work:1.0", "done, deploy unblocked")
        if not result.ok:
            log.warning("could not notify pane", error=result.error, msg=result.message)

API
---
* :func:`inject` — type ``text`` into a pane and press Enter (the headline call).
* :func:`send_keys` — the low-level primitive (``enter`` defaults to ``False``); send tmux
  key *names* with ``literal=False``.
* :func:`has_session` — pre-flight check that a session exists.
* :func:`list_panes` — enumerate panes (optionally within a session/window).
* :func:`resolve_target` — parse ``session:window.pane`` / ``%paneid`` into a :class:`Target`.
* :class:`InjectResult` — the never-raises outcome (``.ok`` to branch on; ``.error`` is one
  of the ``ERR_*`` sentinels — ``ERR_NO_TMUX``/``ERR_NO_SERVER``/``ERR_BAD_TARGET``/
  ``ERR_SEND_FAILED``/``ERR_TIMEOUT``; ``.argv`` is the command(s) run).

Literal vs interpreted
----------------------
``literal=True`` (the default) sends bytes verbatim (``tmux send-keys -l``), so a message
containing ``Enter`` or ``C-c`` is typed as text. ``literal=False`` interprets the argument
as tmux key *names*. ``enter=True`` always presses a *real* Return via a separate,
interpreted ``send-keys Enter`` call (not a literal LF byte).

Safety posture
--------------
Every runtime/environment failure — tmux not installed, no server running, target pane
gone, send-keys non-zero — degrades to an :class:`InjectResult` with ``ok=False``; it does
NOT raise. Injecting into another agent's pane is a best-effort side-channel, so a
completion hook can call it without a try/except. (Programmer errors — a non-string
``text``/``keys``, an empty/invalid target — still raise, because those are bugs.)
"""

from __future__ import annotations

from .core import (
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

__all__ = [
    "ERR_BAD_TARGET",
    "ERR_NO_SERVER",
    "ERR_NO_TMUX",
    "ERR_SEND_FAILED",
    "ERR_TIMEOUT",
    "InjectResult",
    "Target",
    "has_session",
    "inject",
    "list_panes",
    "resolve_target",
    "send_keys",
]

__version__ = "0.1.0"
