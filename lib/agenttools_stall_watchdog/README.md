# agenttools-stall-watchdog

Tiered **warn-then-abort** stall detection for a long-running process by watching **one
file's mtime** — mtime advancing is progress, mtime frozen is a stall. Works for a
background Claude Code `Agent`-tool subagent's transcript (`subagents/agent-<id>.jsonl`)
**or** any other long-running run's output/log — an e2e/Playwright suite, `npm test`, an
Electron-launch script — anything that grows while it's actually making progress. Built
from a real incident (agent-tools#205): a background e2e/screenshot run sat stalled for
~2 hours because the orchestrator was only checking manually every 20-30 minutes via
`ScheduleWakeup`. This is the automated replacement for that manual polling.

## The two tiers — and who each one talks to

| Tier | Default threshold | What fires | Audience / language |
| --- | --- | --- | --- |
| **WARN** | 5 min (`--warn-after 300`) no progress | prints a `WARN` line; best-effort tmux pane nudge (`--tmux-target`) | the **agent/orchestrator's own pane** — ENGLISH |
| **ABORT** | 30 min (`--abort-after 1800`) no progress | prints an `ABORT` line; SIGTERM→SIGKILL a real pid's PROCESS GROUP (`--pid`, if known); tmux nudge (English) **+** a `tg` alert | tmux nudge → the agent (English); `tg` alert → **Alex, the human** — RUSSIAN |

This tier/channel split is deliberate and came from two rounds of Alex's own feedback on a
live test of this tool (tg#6962, tg#6967): **Tier-1 is agent-facing only** — it is
delivered by injecting a line into the watching agent/orchestrator's own tmux pane (via
`agenttools_tmux_inject`, in English, because the point is to make the agent itself react,
not to report to a human) and by default sends **nothing** to Alex over `tg`. **Only
Tier-2/abort reaches Alex**, over `tg`, in Russian, using an explicit-field template (see
below). Pass `--tg-on-warn` to also ping Alex at the warn tier (e.g. when there is no tmux
target to nudge at all).

Both thresholds are configurable per invocation. **The production defaults are exactly
Alex's original ask (tg#6942): 5 minutes / 30 minutes — never change these without a new,
explicit instruction.** For a smoke test, scale them down (e.g. `--warn-after 8
--abort-after 20`) **and always pass `--test`** — every test alert then carries a mandatory
marker line plus the exact scaled threshold used and a note that production defaults to
5m/30m (Alex tg#6973: an unmarked test alert that fired after 21 real seconds read as an
absurdly broken threshold, not as a test artifact — it wasn't, but the alert didn't say so).

**Why 5 minutes and not less: a subagent can legitimately be thinking.** The mtime signal
only moves when a transcript/log actually gets a new line — a long reasoning turn or one
slow tool call inside the 5-minute window is normal work, not a stall, and correctly
produces no WARN at all. The threshold exists to catch a transcript that stays frozen
*across* that window, not to interrupt normal latency.

## The honest limit — read this before wiring a new caller

An in-session Claude Code `Agent`-tool subagent has **no OS process and no tmux pane**.
Verified directly: a live subagent's `.meta.json` sidecar carries only
`{agentType, description, toolUseId}` — no pid — and `ps` shows no per-subagent process
(these subagents run in-process inside the harness, not as separate `claude` invocations).
So for that case there is genuinely **no external handle to abort it with**. Only the
orchestrating agent's own `TaskStop` tool can. This module does not fake an abort it
cannot perform: its ABORT tier always prints **and** tmux-nudges **and** `tg`-alerts the
diagnostic pointer (watched-file path + `stat`/`tail` commands + `SendMessage`/`TaskStop`
hint) — that pointer IS the abort action here, because reaching the live orchestrator (or
Alex) is the only thing actually possible. Where a real pid IS known — a standalone
`claude` session in its own tmux pane/worktree, or any long-running e2e/test process
launched as a real OS process (`npm test`, a Playwright runner, an Electron launch) —
`--pid` performs a real SIGTERM→SIGKILL of the pid's process group (falling back to the bare pid), so an `npm`/shell wrapper's hung children die with it.

**Practical consequence: pick the deployment mode by which pattern you're watching.**

1. **In-session `Agent`-tool subagent** (no pid) — run this as a `Monitor` command from
   inside the live orchestrator session. Its WARN/ABORT lines become `Monitor`
   notifications with zero polling on the orchestrator's side; the orchestrator is the one
   that acts (check the transcript, `TaskStop`, re-dispatch).
2. **A long-running e2e/test/build process with a real pid** — a standalone `claude`
   session in a tmux pane, an `npm test` / Playwright suite, an Electron-launch script —
   run this as a detached background daemon (`nohup ... &`) with `--pid <its pid>` and
   `--tmux-target <the watching agent's pane>`; it can nudge and forcibly abort on its
   own, no orchestrator turn required.

## Quick start — as a library (pure logic, no side effects)

```python
import os, time
from agenttools_stall_watchdog import Watchdog

wd = Watchdog(
    "/path/to/subagents/agent-xyz.jsonl",   # or any e2e run's output/log file
    warn_after=300, abort_after=1800,
    clock=time.time,
    get_mtime=lambda p: os.stat(p).st_mtime if os.path.exists(p) else None,
)

event = wd.poll()   # None on every call unless a tier was just entered/left
if event and event.tier == "warn":
    ...
```

`Watchdog.poll()` is **edge-triggered**: it returns an `Event` only on a threshold
crossing (entering WARN, entering ABORT, or recovering back to `ok`), never once per tick
while a tier is merely still active — and a stall that recovers (mtime advances again)
resets the episode, so a later stall re-fires WARN/ABORT instead of staying silent
forever.

## Quick start — as a CLI (real side effects)

```sh
python -m agenttools_stall_watchdog watch \
  --watch-file ~/.claude/projects/<proj>/<session>/subagents/agent-<id>.jsonl \
  --agent-id a1601ca6 --description "screenshot recapture" \
  --warn-after 300 --abort-after 1800 --poll-interval 15 \
  --tmux-target %0 \
  --tg-agent claude
```

Watching an e2e run's log instead of a subagent transcript is the same flag, a different path:

```sh
python -m agenttools_stall_watchdog watch \
  --watch-file /tmp/e2e-run.log --pid "$(cat /tmp/e2e-run.pid)" \
  --tmux-target %0 --warn-after 300 --abort-after 1800
```

As a `Monitor` command (in-session, no side effects needed — the printed lines alone are
the notification):

```sh
python -m agenttools_stall_watchdog watch --watch-file <path> --dry-run \
  --warn-after 300 --abort-after 1800
```

(`Monitor` treats each stdout line as one event; `--dry-run` skips tmux/tg/kill since the
orchestrator itself is the one reacting to the notification.)

As a standalone background daemon that can act on its own (pattern 2 above):

```sh
nohup python -m agenttools_stall_watchdog watch \
  --watch-file <path> --pid <pid> --tmux-target <pane> \
  --warn-after 300 --abort-after 1800 > watchdog.log 2>&1 &
```

Smoke-testing with scaled-down thresholds (always add `--test`):

```sh
python -m agenttools_stall_watchdog watch --watch-file <path> --pid <dummy-pid> \
  --tmux-target <pane> --warn-after 8 --abort-after 20 --poll-interval 2 --test
```

## CLI reference

```
agenttools-stall-watchdog watch
  --watch-file PATH          (required, alias --transcript) the file whose mtime signals
                              progress — a subagent transcript OR any e2e/test/build log
  --agent-id ID              agent id — used in messages + the SendMessage/TaskStop hint
  --description TEXT         human label included in messages
  --warn-after SECONDS       default 300 (5 min) — production default, do not lower without instruction
  --abort-after SECONDS      default 1800 (30 min) — production default, do not lower without instruction
  --poll-interval SECONDS    default 15
  --max-runtime SECONDS      stop watching after this long regardless of state (default: unbounded)
  --pid PID                  SIGTERM→SIGKILL this pid's process group on ABORT (only when a real pid is known)
  --tmux-target TARGET       tmux pane/session (e.g. %3 or work:1.0) to nudge — English,
                              agent-facing — on WARN and ABORT
  --tg-agent NAME            forwarded as `tg --agent NAME` so the Alex-facing tg alert routes to a pane
  --no-tg                    skip the tg alert entirely
  --tg-on-warn                also send the Russian, Alex-facing tg alert on WARN (default: tg fires
                              on ABORT only; Tier-1 is agent-facing-only via --tmux-target)
  --dry-run                  print crossings only, run no side effects
  --once                     poll exactly once and exit (scripting/tests)
  --test                     mark every message as a test run (mandatory marker + the exact
                              scaled threshold used + "prod default 5m/30m")
```

Exit code `2` on an ABORT crossing (so a caller scripting around this can branch on it);
`0` otherwise (including `--once` with no crossing, or `--max-runtime` elapsing quietly).

## What it reuses (deliberately, not reinvented)

- **[`agenttools_tmux_inject`](../agenttools_tmux_inject)** — the WARN/ABORT tmux nudge
  is the exact "post a line into another agent's interactive pane" primitive already
  extracted from tg-ctl, not a new implementation of tmux target parsing or
  literal-vs-interpreted `send-keys`.
- **the `tg` CLI** — the Alex-facing abort alert shells out to it (`tg --tag problem
  "..."`), the ecosystem's one sanctioned channel for an agent to push a status/problem
  report. This module never talks to the Telegram Bot API directly.
- **the mtime-liveness convention already in use** — `~/.claude/projects/<proj>/<session>/
  subagents/agent-<id>.jsonl` mtime advancing = progress is the same signal this
  session's own orchestrator was already checking by hand (`stat -f "%Sm %N" <path>`);
  this module automates exactly that check, and generalizes it to any watched file.

Deliberately NOT reused: `agenttools_daemon`'s `Supervisor`. That class supervises a
**child process this tool spawns** (pidfile ownership, restart-with-backoff); a stall
watchdog observes a **sibling** process/subagent it did not spawn and, in the in-session
case, cannot even signal — a different shape of problem, so `pid_kill` here is a plain
SIGTERM→SIGKILL against an externally-supplied pid's process group, not a `Supervisor`.

## Public API

| Symbol | Purpose |
| --- | --- |
| `Watchdog(transcript_path, *, warn_after=300.0, abort_after=1800.0, clock, get_mtime)` | stateful poller; `.poll() -> Event \| None` |
| `Event(tier, elapsed, mtime, transcript_path, recovered)` | one threshold crossing |
| `classify(elapsed, warn_after, abort_after) -> "ok" \| "warn" \| "abort"` | pure classification |
| `mtime_or_none(path) -> float \| None` | the real `get_mtime` (`os.stat`, `None` if missing) |
| `build_diagnostics_message(event, *, agent_id=None, description=None, pid=None, is_test=False, warn_after=None, abort_after=None) -> str` | the Russian, Alex-facing message body (tg only) |
| `build_nudge_line(event, *, agent_id=None, description=None, pid=None, is_test=False, warn_after=None, abort_after=None) -> str` | the English, agent-facing single-line nudge (tmux only) |
| `tmux_nudge(target, *, agent_id=None, description=None, pid=None, is_test=False, warn_after=None, abort_after=None, timeout=5.0) -> Action` | inject the English nudge into a pane |
| `tg_alert(config=None, *, agent_id=None, description=None, pid=None, is_test=False, warn_after=None, abort_after=None) -> Action` | send the Russian diagnostics message via `tg` |
| `pid_kill(pid, *, stop_grace=5.0, sleeper=time.sleep) -> Action` | SIGTERM→SIGKILL the pid's process group (fallback: bare pid) |
| `broadcast(*actions) -> Action` | combine actions; one raising doesn't skip the rest |
| `TgAlertConfig(tg_bin="tg", agent=None, tag="problem", timeout=15.0)` | `tg_alert` config |

## Testability

`Watchdog` takes `clock` and `get_mtime` as required, injected parameters — same seam
discipline as `agenttools_daemon` (fake spawner/clock/sleeper) — so the classification and
edge-triggering logic is tested with a fake clock and a fake mtime source: no real files,
no real sleeps, deterministic threshold-crossing assertions. `pid_kill` takes an injectable
`sleeper` for the same reason. `tmux_nudge` / `tg_alert` are thin wrappers around
already-tested externals (`agenttools_tmux_inject`, the `tg` binary); their message
CONTENT is unit-tested against fakes, and the real integration (a real tmux pane, a real
`tg` send, a real pid kill) is covered by a live smoke test with scaled-down thresholds
(always pass `--test`) rather than re-mocked in the unit suite — see
`tests/test_agenttools_stall_watchdog.py`. **The live smoke test caught two real bugs a
unit-only suite missed**: (1) a multi-line message injected into a live tmux pane gets
each line EXECUTED as a separate shell command (fixed by making the tmux nudge always a
single line — `build_nudge_line`, never `build_diagnostics_message`), and (2)
`agent_id`/`description` weren't threaded through to the nudge at all. Regression tests
for both now exist, but treat "the unit suite is green" as necessary, not sufficient, for
any future change to the delivery actions — re-run the live smoke test too.

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the
`agenttools-stall-watchdog` distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-stall-watchdog"]
```

```sh
pip install -e /path/to/agent-tools/lib/agenttools_stall_watchdog   # editable install
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_stall_watchdog \
  --with /path/to/agent-tools/lib/agenttools_tmux_inject \
  python -m agenttools_stall_watchdog watch --watch-file <path> --once
```
