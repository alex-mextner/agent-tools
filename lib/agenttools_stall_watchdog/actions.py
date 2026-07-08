"""Side-effect actions fired on a stall :class:`~agenttools_stall_watchdog.core.Event`.

Kept separate from ``core.py`` on purpose: ``core`` is pure classification logic tested
with fakes and zero I/O; this module is where the actual tmux/subprocess/signal calls
live, each wrapped to degrade to a logged failure rather than raise (same "never raises on
the operational path" doctrine as ``agenttools_tmux_inject`` itself) — a watchdog whose own
notifier crashes must not take down the poll loop that is its one job.

The honest limit (read this before wiring a new caller)
---------------------------------------------------------
An in-session Claude Code ``Agent``-tool subagent has **no OS process and no tmux pane** —
confirmed by inspecting a live subagent's ``.meta.json`` sidecar (agentType/description/
toolUseId only, no pid) and by ``ps`` showing no per-subagent process. So for THAT case
(the one that triggered this ticket): ``pid_kill`` has nothing to kill, and a tmux nudge
has nowhere to land. ``build_diagnostics_message`` is what actually carries the payload in
that case — it names the transcript path and the exact follow-up commands (``stat``,
``tail``, ``SendMessage``/``TaskStop``) so a human or the orchestrating agent can act,
because nothing else can act on its behalf. ``pid_kill`` and ``tmux_nudge`` are real,
effective actions for the OTHER pattern this ecosystem also uses — a standalone ``claude``
session running in its own tmux pane/worktree, which DOES have both.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .core import Event

Action = Callable[[Event], None]


def _who_ru(agent_id: Optional[str], description: Optional[str]) -> str:
    """The "what's stuck" label for the Russian, Alex-facing message (tg_alert)."""
    if description and agent_id:
        return f"{description} ({agent_id})"
    if description:
        return description
    if agent_id:
        return f"agent {agent_id}"
    return "фоновый процесс (agent_id не передан)"


def _who_en(agent_id: Optional[str], description: Optional[str]) -> str:
    """The "what's stuck" label for the English, agent-facing nudge (tmux_nudge)."""
    if description and agent_id:
        return f"{description} ({agent_id})"
    if description:
        return description
    if agent_id:
        return f"agent {agent_id}"
    return "watched process"


def _test_threshold_note_ru(warn_after: Optional[float], abort_after: Optional[float]) -> Optional[str]:
    """The Russian test-threshold annotation, or ``None`` when no thresholds were given.

    Alex's tg#6973 feedback, on a live smoke-test alert that used a scaled-down 8s/20s
    threshold with no indication it was a test value: a real subagent can legitimately
    spend minutes reasoning or inside one long tool call with no transcript write — that's
    thinking, not a stall — so seeing a real-looking alert fire after 21 seconds read as a
    broken/absurdly tight threshold, not a test artifact. Every test alert must now say
    EXACTLY which threshold fired and that production's default is 5min/30min, so the
    numbers are never mistaken for the real policy.
    """
    if warn_after is None or abort_after is None:
        return None
    return (
        f"Тестовый порог: warn={warn_after:.0f}с / abort={abort_after:.0f}с "
        f"(test-only value, боевой дефолт — 5 мин / 30 мин, оба настраиваются "
        f"--warn-after/--abort-after)"
    )


def build_diagnostics_message(
    event: Event,
    *,
    agent_id: Optional[str] = None,
    description: Optional[str] = None,
    pid: Optional[int] = None,
    is_test: bool = False,
    warn_after: Optional[float] = None,
    abort_after: Optional[float] = None,
) -> str:
    """The human-readable pointer to full diagnostics for ``event`` (Russian — Alex-facing,
    delivered ONLY via :func:`tg_alert`, and by default only on the ABORT tier — see
    ``cli._build_action``: Alex's tg#6967 correction was explicit that this channel is
    Tier-2/abort only, never Tier-1/warn, and never the tmux-injected agent-facing nudge).

    Every field Alex's feedback (tg#6962, on a real live-alert message that reached him and
    read as unreadable) asked for, explicit and labelled: что застряло / сколько простоя /
    что сделал watchdog / где смотреть диагностику / что делать человеку. A test/smoke run
    MUST pass ``is_test=True`` — it becomes the mandatory first line so a human reading it
    never has to guess whether a real process was just aborted. Pass ``warn_after``/
    ``abort_after`` (the CONFIGURED thresholds, not ``event.elapsed``) so a test alert also
    names which scaled-down threshold fired — see :func:`_test_threshold_note_ru`.
    """
    minutes = event.elapsed / 60.0
    tier_ru = "ОСТАНОВКА (ABORT)" if event.tier == "abort" else "ПРЕДУПРЕЖДЕНИЕ (WARN)"
    lines = []
    if is_test:
        lines.append("ТЕСТ — действий не нужно (тестовый прогон watchdog'а)")
        note = _test_threshold_note_ru(warn_after, abort_after)
        if note:
            lines.append(note)
    lines.append(f"[stall-watchdog] {tier_ru}")
    lines.append(f"Что застряло: {_who_ru(agent_id, description)}")
    lines.append(
        f"Сколько простоя: {minutes:.1f} мин ({event.elapsed:.0f}с), "
        f"порог сработал только что"
    )
    if event.tier == "abort":
        if pid is not None:
            # "попытался" deliberately: pid_kill runs first in the abort broadcast but
            # swallows already-dead/EPERM outcomes, so the message must not overclaim a
            # kill that may have been a no-op (review finding).
            lines.append(
                f"Что сделал watchdog: попытался остановить PID {pid} и его группу "
                f"процессов (SIGTERM, затем SIGKILL); проверь `ps -p {pid}`"
            )
        else:
            lines.append(
                "Что сделал watchdog: PID неизвестен (скорее всего внутрисессионный "
                "Agent-tool сабагент — у него нет отдельного OS-процесса) — сам процесс НЕ "
                "остановлен, watchdog только сообщает"
            )
    else:
        lines.append("Что сделал watchdog: отправил предупреждение, процесс не трогал")
    # shlex.quote: these two "Диагностика" lines are COMMANDS the reader is expected to
    # copy-paste into a shell. An unquoted path containing a space/;/# (review finding:
    # `/tmp/a; echo OWNED #.log`) would otherwise execute an embedded payload on paste.
    quoted = shlex.quote(event.transcript_path)
    lines.append(f"Файл (лог/транскрипт): {event.transcript_path}")
    lines.append(f'Диагностика: stat -f "%Sm %N" {quoted}')
    lines.append(f"              tail -c 4000 {quoted}")
    if agent_id:
        lines.append(
            f"Что делать: SendMessage агенту {agent_id}, чтобы проверить, "
            f"или TaskStop({agent_id!r}), чтобы остановить."
        )
    else:
        lines.append(
            "Что делать: agent_id не передан watchdog'у — определи процесс/сабагента по пути "
            "к файлу (subagents/agent-<id>.jsonl для сабагента, иначе — по логу e2e-прогона), "
            "затем действуй вручную."
        )
    return "\n".join(lines)


def build_nudge_line(
    event: Event,
    *,
    agent_id: Optional[str] = None,
    description: Optional[str] = None,
    pid: Optional[int] = None,
    is_test: bool = False,
    warn_after: Optional[float] = None,
    abort_after: Optional[float] = None,
) -> str:
    """A SHORT, single-line, ENGLISH, agent-facing nudge for :func:`tmux_nudge` — never
    :func:`build_diagnostics_message` (Russian, Alex-facing, tg-only).

    Language and audience are deliberate, per Alex's tg#6967 correction: this line is
    injected into the ORCHESTRATOR/agent's own tmux pane — an agent-facing signal meant to
    make the agent itself react, not a human report — so it stays in English regardless of
    the surrounding session's chat language. Only :func:`build_diagnostics_message`
    (delivered over `tg`, to Alex, abort-tier only by default) is Russian.

    A tmux pane targeted by ``tmux_nudge`` is a LIVE, active shell — ``agenttools_tmux_inject``
    presses one real Return after the injected text (see its own docs on ``enter=True``), but
    every EMBEDDED newline *inside* the text is still typed as a literal LF byte into that
    shell, which most shells (zsh/bash) treat as their own Return: each line of a multi-line
    payload gets submitted and EXECUTED as its own command. Confirmed the hard way in this
    ticket's live smoke test — a `build_diagnostics_message()` string injected into a real pane
    caused ``tail -c 4000 <path>`` (one of the message's OWN lines) to actually run in that
    shell. The multi-line diagnostics block is safe over `tg` (a text message, not a live
    shell) and on stdout (a log line); it must never be typed into an interactive pane.
    """
    minutes = event.elapsed / 60.0
    tier_en = "ABORTED" if event.tier == "abort" else "stall warning"
    basename = event.transcript_path.rsplit("/", 1)[-1]
    # "attempted kill" not "killed": pid_kill swallows already-dead/EPERM outcomes, so the
    # nudge must not overclaim a kill that may have been a no-op (review finding).
    action_en = (
        f", attempted kill of PID {pid} (+group)"
        if (event.tier == "abort" and pid is not None)
        else ""
    )
    if is_test:
        thresholds = (
            f" [test-only threshold warn={warn_after:.0f}s/abort={abort_after:.0f}s, "
            f"prod default 5m/30m]"
            if warn_after is not None and abort_after is not None
            else ""
        )
        prefix = f"TEST, no action needed{thresholds}: "
    else:
        prefix = ""
    line = (
        f"{prefix}[stall-watchdog] {tier_en}: {_who_en(agent_id, description)} — "
        f"no progress for {minutes:.1f}min ({basename}){action_en}; check stdout/tg for details."
    )
    # The single-line invariant is THIS function's own contract, not something callers
    # (tmux_nudge) should have to re-enforce — a caller-supplied `description` with an
    # embedded newline must not silently break it (see the module docstring on why a
    # multi-line payload in a live tmux pane is a real hazard, not just an eyesore).
    return " ".join(line.splitlines())


def tmux_nudge(
    target: str,
    *,
    agent_id: Optional[str] = None,
    description: Optional[str] = None,
    pid: Optional[int] = None,
    is_test: bool = False,
    warn_after: Optional[float] = None,
    abort_after: Optional[float] = None,
    timeout: float = 5.0,
) -> Action:
    """Best-effort: inject a one-line nudge into ``target``'s tmux pane.

    Reuses ``agenttools_tmux_inject`` (already extracted from tg-ctl for exactly this
    "post a line into another agent's interactive pane" use case) rather than
    reimplementing pane targeting / literal-vs-interpreted send-keys handling. Import is
    deferred so importing this module doesn't require tmux to be on PATH.

    Deliberately sends :func:`build_nudge_line`, NOT :func:`build_diagnostics_message` — see
    that function's docstring for why a multi-line payload must never be typed into a live
    shell pane. The ``.replace("\\n", " ")`` below is defense in depth (should the message
    ever gain an embedded newline some other way), not the primary safeguard.
    """

    def _action(event: Event) -> None:
        from agenttools_tmux_inject import inject

        message = build_nudge_line(
            event,
            agent_id=agent_id,
            description=description,
            pid=pid,
            is_test=is_test,
            warn_after=warn_after,
            abort_after=abort_after,
        )
        inject(target, message.replace("\n", " "), timeout=timeout)
        # inject() never raises (InjectResult.ok=False on any environment failure) — the
        # watchdog loop does not need to branch on the outcome; a failed nudge is a
        # best-effort side-channel miss, not a reason to stop watching.

    return _action


@dataclass(frozen=True)
class TgAlertConfig:
    """Config for :func:`tg_alert` — kept as a value object so tests can assert on it."""

    tg_bin: str = "tg"
    agent: Optional[str] = None  # forwarded as `tg --agent <name>` (routes to a specific pane)
    tag: str = "problem"
    timeout: float = 15.0


def tg_alert(
    config: Optional[TgAlertConfig] = None,
    *,
    agent_id: Optional[str] = None,
    description: Optional[str] = None,
    pid: Optional[int] = None,
    is_test: bool = False,
    warn_after: Optional[float] = None,
    abort_after: Optional[float] = None,
) -> Action:
    """Best-effort: send the diagnostics message via the ``tg`` CLI.

    ``tg`` is send-only and already the ecosystem's standard channel for an agent to push
    a status/problem report (see the ``tg`` skill) — reused here rather than talking to
    the Telegram Bot API directly (categorically against ecosystem convention: "Never curl
    Telegram directly"). Subprocess failure (binary missing, non-zero exit, timeout)
    degrades to a swallowed, non-raising no-op — same operational-path doctrine as
    ``agenttools_tmux_inject``: a broken notifier must not crash the poll loop whose job is
    to keep watching. Unlike :func:`tmux_nudge`, a `tg` message is a plain text message, not
    a live shell — the full multi-line :func:`build_diagnostics_message` is safe here.
    """
    cfg = config or TgAlertConfig()

    def _action(event: Event) -> None:
        message = build_diagnostics_message(
            event,
            agent_id=agent_id,
            description=description,
            pid=pid,
            is_test=is_test,
            warn_after=warn_after,
            abort_after=abort_after,
        )
        argv = [cfg.tg_bin, "--tag", cfg.tag]
        if cfg.agent:
            argv += ["--agent", cfg.agent]
        # tg treats any unknown dashed token as an ERROR (it has no `--` end-of-options
        # marker), so a message that ever started with `-` would kill the alert. Today's
        # templates start with `[`/`ТЕСТ`, but a template edit must not silently break
        # delivery — pad defensively (review finding).
        argv.append(" " + message if message.startswith("-") else message)
        try:
            subprocess.run(
                argv,
                capture_output=True,
                timeout=cfg.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return _action


def pid_kill(pid: int, *, stop_grace: float = 5.0, sleeper=time.sleep) -> Action:
    """Real abort: SIGTERM the pid's PROCESS GROUP, escalate to SIGKILL after ``stop_grace``.

    Only meaningful when the caller actually has a PID — a standalone ``claude`` session
    in its own tmux pane, or any backgrounded shell command with a real OS process. An
    in-session ``Agent``-tool subagent has no PID (see the module docstring); do not call
    this action for that case, it has nothing to signal.

    Signals the whole process GROUP (``os.killpg(os.getpgid(pid), sig)``), not just the
    single pid, falling back to a plain ``os.kill`` when the group can't be resolved or
    signalled (already-dead leader, or an EPERM group containing processes we don't own).
    Review finding: for the documented ``npm test`` / Playwright / Electron-launch use
    case the given pid is usually a shell/npm WRAPPER — killing only it leaves the real
    hung children running, which is exactly the silent non-abort this tool exists to
    prevent. (A backgrounded shell pipeline gets its own process group, so the group is
    the whole run.)

    Escalation mirrors ``agenttools_daemon.Supervisor.stop()`` (SIGTERM, wait, SIGKILL if
    still alive) — same shape, not imported, because this acts on an arbitrary external
    pid rather than a child this process spawned (no pidfile, no ownership to share).
    """
    import os
    import signal

    def _alive(p: int) -> bool:
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _resolve_pgid() -> Optional[int]:
        """The pid's process group to signal, or ``None`` for bare-pid mode.

        Never returns a group we ourselves belong to: with the documented
        `nohup watchdog & ; run &` launch pattern in a non-interactive shell, the
        watched pid can share the watchdog's OWN process group — a group kill would
        then take down the watchdog and every sibling job (review finding).
        """
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return None
        return pgid if pgid != os.getpgrp() else None

    def _signal_group(pgid: int, sig: int) -> bool:
        """Signal (or, with sig=0, liveness-probe) the group; True if it has live members."""
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            # EPERM still means SOMETHING in the group exists; report it as live so the
            # caller escalates rather than silently declaring the run gone.
            return True

    def _signal_pid(sig: int) -> bool:
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _action(event: Event) -> None:
        # pid<=0 has special kill() semantics (0 = OUR whole process group, negative =
        # "the group -pid") — a pidfile that read `0` must be a refused no-op, never a
        # self-kill (review finding). The CLI also rejects it at parse time; this guard
        # protects direct library callers.
        if pid <= 0:
            return
        # Resolve the group ONCE, before SIGTERM: the leader pid can exit during the
        # grace period, after which os.getpgid(pid) is unresolvable even while its
        # children live on.
        pgid = _resolve_pgid()
        if pgid is not None:
            if not _signal_group(pgid, signal.SIGTERM):
                return
            sleeper(stop_grace)
            # Escalate on GROUP liveness, never on the leader pid alone: a wrapper/npm
            # leader routinely exits on SIGTERM while a hung child (the very process this
            # abort exists for) ignores it and survives — gating SIGKILL on _alive(pid)
            # would skip the escalation exactly then (review finding; the same
            # leader-vs-group liveness gap as hyperide HYP-926).
            if _signal_group(pgid, 0):
                _signal_group(pgid, signal.SIGKILL)
            return
        if not _signal_pid(signal.SIGTERM):
            return
        sleeper(stop_grace)
        if _alive(pid):
            _signal_pid(signal.SIGKILL)

    return _action


def broadcast(*actions: Action) -> Action:
    """Combine several actions into one, each isolated: one raising does not skip the rest."""

    def _action(event: Event) -> None:
        for action in actions:
            try:
                action(event)
            except Exception:  # noqa: BLE001 - a notifier's own bug must not break the others
                pass

    return _action
