"""agenttools_stall_watchdog — tiered stall detection for a long-running process.

Polls one file's mtime (the ecosystem's established liveness signal — mtime advancing is
progress, mtime frozen is a stall) and fires two tiers when it goes stale: a WARN (default
5 minutes) and an ABORT (default 30 minutes). The watched file is either a background
Claude Code ``Agent``-tool subagent's transcript, OR any other long-running run's
output/log — an e2e/Playwright suite, ``npm test``, an Electron-launch script — the
mechanism doesn't care which. Built in direct response to a real incident
(agent-tools#205): a background e2e/screenshot run sat stalled for ~2 hours because the
orchestrator was only checking manually every 20-30 minutes.

Two tiers, two different audiences (per Alex's tg#6962/tg#6967 corrections on a live test)
--------------------------------------------------------------------------------------------
* **Tier-1 (WARN)** is **agent-facing only**: a short ENGLISH line injected into the
  watching agent/orchestrator's own tmux pane via ``agenttools_tmux_inject`` — meant to
  make the agent itself react, not to report to a human. Nothing reaches Alex by default.
* **Tier-2 (ABORT)** additionally reaches Alex, over the ``tg`` CLI, in RUSSIAN, using an
  explicit-field template (что застряло / сколько простоя / что сделал watchdog / где
  диагностика / что делать) — his own corrected format after the first version proved
  unreadable in a live test.

Production defaults are exactly Alex's original ask (tg#6942): 5 minutes / 30 minutes.
Any test run MUST pass ``is_test=True`` (CLI: ``--test``) so every alert says explicitly
which threshold fired and that production defaults to 5m/30m (tg#6973: an unmarked test
alert that fired after 21 real seconds read as an absurdly broken policy, not a test).

Why this exists — the honest constraint that shaped the design
-----------------------------------------------------------------
An in-session Claude Code ``Agent``-tool subagent has **no OS process and no tmux pane** —
verified directly (its ``.meta.json`` sidecar carries no pid; ``ps`` shows no per-subagent
process). So there is no external handle to forcibly abort it with; only the orchestrating
agent itself can (via the harness's own ``TaskStop`` tool). This module does NOT pretend
otherwise: its ABORT tier always prints and alerts the diagnostic pointer (watched-file
path + inspect commands + a SendMessage/TaskStop hint) — that pointer IS the abort action
for this case, because it is the only thing that can actually reach a human or the live
orchestrator. Where a real OS pid IS known (a standalone ``claude`` session in its own
tmux pane/worktree, or any e2e/test/build process launched as a real OS process), ``--pid``
additionally SIGTERMs then SIGKILLs it for real.

What it reuses (deliberately, not reinvented)
------------------------------------------------
* **agenttools_tmux_inject** — the Tier-1 pane nudge is the same "post a line into
  another agent's interactive pane" primitive already extracted from tg-ctl, not a new
  implementation of tmux target parsing / literal-vs-interpreted send-keys.
* **the `tg` CLI** — the Tier-2 Alex-facing alert is sent through it (`tg --tag problem
  "..."`), the ecosystem's one sanctioned channel for an agent to push a status/problem
  report; this module never talks to the Telegram Bot API directly.

Quick start
-----------
    from agenttools_stall_watchdog import Watchdog, classify
    import time

    wd = Watchdog(
        "/path/to/subagents/agent-xyz.jsonl",
        warn_after=300, abort_after=1800,
        clock=time.time, get_mtime=lambda p: os.stat(p).st_mtime,
    )
    event = wd.poll()   # None, or an Event on a threshold crossing

Or from the shell, as a standalone loop that also fires side effects:

    python -m agenttools_stall_watchdog watch \\
        --transcript ~/.claude/projects/<proj>/<session>/subagents/agent-<id>.jsonl \\
        --agent-id a1601ca6 --tmux-target %0 --warn-after 300 --abort-after 1800

See ``lib/agenttools_stall_watchdog/README.md`` for the full reference.
"""

from __future__ import annotations

from .actions import (
    Action,
    TgAlertConfig,
    broadcast,
    build_diagnostics_message,
    build_nudge_line,
    pid_kill,
    tg_alert,
    tmux_nudge,
)
from .core import Event, Tier, Watchdog, classify, mtime_or_none

__all__ = [
    "Action",
    "Event",
    "Tier",
    "TgAlertConfig",
    "Watchdog",
    "broadcast",
    "build_diagnostics_message",
    "build_nudge_line",
    "classify",
    "mtime_or_none",
    "pid_kill",
    "tg_alert",
    "tmux_nudge",
]

__version__ = "0.1.0"
