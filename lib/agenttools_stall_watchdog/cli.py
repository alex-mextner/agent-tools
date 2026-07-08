"""``agenttools-stall-watchdog watch`` — poll a transcript, act on threshold crossings.

Two deployment modes share this ONE loop (the design the ticket asked for — "one core,
two deployment modes" rather than an ephemeral test-only script):

* **In-session, as a ``Monitor`` command** — the orchestrator runs this under the harness's
  own ``Monitor`` tool (``persistent: true``); every WARN/ABORT line printed to stdout is a
  push notification back to the orchestrator with no polling on its side. This is the
  "process" fix: it replaces manually-scheduled ``ScheduleWakeup`` re-checks.
* **Standalone background daemon** — launched detached (``nohup ... &`` / ``run_in_background``)
  next to a long-running dispatch; it also performs the real side effects (tmux nudge, tg
  alert, PID kill) since nothing else is watching it. This is the "harness" fix.

Every WARN/ABORT/RECOVER crossing is printed as one line (``[stall-watchdog] TIER ...``) —
that line IS the Monitor-consumable event; the side-effect actions (tmux/tg/kill) are
in addition to it, not instead of it, so the standalone-daemon mode's log/stdout is a
complete record even if every side effect happens to be a no-op (e.g. no PID given).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import actions as actions_mod
from .core import Event, Watchdog, mtime_or_none


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agenttools-stall-watchdog",
        description="Watch a background subagent's transcript for staleness; warn then abort.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="poll one file until aborted or interrupted")
    watch.add_argument(
        "--watch-file",
        "--transcript",
        dest="transcript",
        required=True,
        help="path to the file whose mtime signals progress — a subagent's "
        "subagents/agent-<id>.jsonl transcript, OR any other long-running process's "
        "output/log (an e2e/Playwright suite's log, npm test output, an Electron-launch "
        "log — anything that grows while its process is actually making progress). "
        "`--transcript` is a backward-compatible alias for the same flag.",
    )
    watch.add_argument("--agent-id", default=None, help="agent id, for messages and SendMessage/TaskStop hints")
    watch.add_argument("--description", default=None, help="human label for messages (task description)")
    watch.add_argument("--warn-after", type=float, default=300.0, help="seconds of no progress before WARN (default 300 = 5min)")
    watch.add_argument("--abort-after", type=float, default=1800.0, help="seconds of no progress before ABORT (default 1800 = 30min)")
    watch.add_argument("--poll-interval", type=float, default=15.0, help="seconds between checks (default 15)")
    watch.add_argument("--max-runtime", type=float, default=None, help="stop watching after this many seconds regardless of state (default: unbounded)")
    watch.add_argument("--pid", type=int, default=None, help="OS pid to SIGTERM/SIGKILL on ABORT, if known (standalone tmux-pane sessions only; in-session Agent-tool subagents have none)")
    watch.add_argument("--tmux-target", default=None, help="tmux pane/session target (e.g. %%3 or work:1.0) to nudge — ENGLISH, agent-facing — on WARN and ABORT")
    watch.add_argument("--tg-agent", default=None, help="forwarded as `tg --agent <name>` so the Alex-facing tg alert routes to a specific pane")
    watch.add_argument("--no-tg", action="store_true", help="skip the tg alert entirely (e.g. offline tests)")
    watch.add_argument(
        "--tg-on-warn",
        action="store_true",
        help="also send the Russian, Alex-facing tg alert on the WARN tier (default: tg fires "
        "on ABORT only — Tier-1 is agent-facing-only via --tmux-target, per Alex tg#6967)",
    )
    watch.add_argument("--dry-run", action="store_true", help="print crossings only, run no side effects (tmux/tg/kill)")
    watch.add_argument("--once", action="store_true", help="poll exactly once and exit (for scripting/tests)")
    watch.add_argument(
        "--test",
        action="store_true",
        help="mark every tmux/tg message as a test run (mandatory test-marker first line, "
        "English on the tmux nudge / Russian 'ТЕСТ — действий не нужно' on the tg alert) — "
        "use for any smoke test that has a real tg/tmux delivery target, so whoever reads "
        "the alert never has to guess whether a real process was aborted",
    )

    return parser


def _build_action(args: argparse.Namespace) -> Optional[actions_mod.Action]:
    if args.dry_run:
        return None
    parts: List[actions_mod.Action] = []
    if args.pid is not None:
        # The kill runs FIRST in the abort broadcast (review finding: the tmux/tg messages
        # describe the kill attempt, so the attempt must have actually happened by the time
        # they are sent — the previous order announced a kill that hadn't run yet).
        # Only fires on ABORT, never on WARN.
        parts.append(_abort_only(actions_mod.pid_kill(args.pid)))
    if args.tmux_target:
        # Tier-1 AND Tier-2 both nudge the agent's own pane (English) — this is the
        # "harness sends a warning to the agent" half of Alex's original ask (tg#6942),
        # confirmed unchanged by his tg#6967 follow-up; only the tg/Russian channel below
        # got tightened to abort-only.
        parts.append(
            actions_mod.tmux_nudge(
                args.tmux_target,
                agent_id=args.agent_id,
                description=args.description,
                pid=args.pid,
                is_test=args.test,
                warn_after=args.warn_after,
                abort_after=args.abort_after,
            )
        )
    if not args.no_tg:
        # Alex's tg#6967 correction: the `tg` channel reaches a HUMAN (Alex) and must stay
        # Tier-2/abort only by default — Tier-1/warn is agent-facing-only (the tmux nudge
        # above). `--tg-on-warn` is the explicit opt-in for a caller that genuinely wants a
        # human ping at the warn tier too (e.g. no tmux target is available at all).
        tg_action = actions_mod.tg_alert(
            actions_mod.TgAlertConfig(agent=args.tg_agent),
            agent_id=args.agent_id,
            description=args.description,
            pid=args.pid,
            is_test=args.test,
            warn_after=args.warn_after,
            abort_after=args.abort_after,
        )
        parts.append(tg_action if args.tg_on_warn else _abort_only(tg_action))
    if not parts:
        return None
    return actions_mod.broadcast(*parts)


def _abort_only(action: actions_mod.Action) -> actions_mod.Action:
    def _wrapped(event: Event) -> None:
        if event.tier == "abort":
            action(event)

    return _wrapped


def _format_line(event: Event, description: Optional[str]) -> str:
    if event.recovered:
        return (
            f"[stall-watchdog] RECOVER: {event.transcript_path} resumed progress "
            f"after {event.elapsed:.0f}s stale"
        )
    label = f" ({description})" if description else ""
    return (
        f"[stall-watchdog] {event.tier.upper()}: {event.transcript_path}{label} "
        f"— {event.elapsed:.0f}s with no transcript progress"
    )


def run_watch(args: argparse.Namespace, *, out=None) -> int:
    """The `watch` subcommand's body — a thin loop over `Watchdog.poll` + printing + actions.

    Split out from `main` so tests can call it directly with a fake clock via monkeypatching
    `time.sleep`/`time.time` at the call sites below, or drive `Watchdog`/`_build_action`
    independently. `out` defaults to `None` (resolved to `sys.stdout` INSIDE the call, not
    at def-time) so a test's `capsys` — which swaps `sys.stdout` per-test — actually
    captures it; a `sys.stdout` default argument would bind the pre-test stream once at
    import time and silently miss every capture.
    """
    if out is None:
        out = sys.stdout
    watchdog = Watchdog(
        args.transcript,
        warn_after=args.warn_after,
        abort_after=args.abort_after,
        clock=time.time,
        get_mtime=mtime_or_none,
    )
    action = _build_action(args)
    started = time.time()
    while True:
        event = watchdog.poll()
        if event is not None:
            print(_format_line(event, args.description), file=out, flush=True)
            if action is not None and not event.recovered:
                action(event)
            if event.tier == "abort" and not event.recovered:
                return 2
        if args.once:
            return 0
        if args.max_runtime is not None and (time.time() - started) >= args.max_runtime:
            return 0
        time.sleep(args.poll_interval)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "watch":
        if args.abort_after < args.warn_after:
            # Surface a misconfiguration as a parser error, not the raw ValueError
            # traceback Watchdog.__init__ would otherwise raise (review finding).
            parser.error(
                f"--abort-after ({args.abort_after:g}) must be >= --warn-after "
                f"({args.warn_after:g})"
            )
        if args.pid is not None and args.pid <= 0:
            # kill(0)/kill(<negative>) have "signal a whole GROUP" semantics — a pidfile
            # that read `0` would otherwise SIGKILL the watchdog's own process group on
            # abort (review finding). Refuse loudly at parse time.
            parser.error(f"--pid must be a positive process id (got {args.pid})")
        try:
            return run_watch(args)
        except KeyboardInterrupt:
            return 130
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
