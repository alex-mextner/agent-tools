# agenttools-daemon

A small, **dependency-free** process supervisor for the agent-tools ecosystem — the one
shared copy of "keep this child alive" that `task-cli` and `tg-ctl` would otherwise each
reimplement (slightly differently, and slightly wrong). **Stdlib only** (no `supervisord`,
no `circus`, no `python-daemon`).

It babysits a single child process: re-spawns it across crashes/kills with exponential
backoff, persists a pidfile that outlives the supervisor itself, and gives you
`start` / `stop` / `status` / `restart` / `run_forever`.

## Features

- **Survives crashes/kills** — `run_forever` re-spawns the child whenever it exits, until
  `max_restarts` is reached or a `stop()` is requested.
- **Exponential backoff between restarts** — `base * factor ** n` capped at `max_delay`, so
  a crash-looping child doesn't hammer the box. Same formula as `agenttools-retry`,
  reproduced here (not imported) to keep this package at zero dependencies.
- **Pidfile as the source of truth** — `start` writes `{pid, started_at, cmd}` JSON
  (`0600`, atomic write+rename); `stop` reads it to signal the child and removes it;
  `status` reads it and liveness-probes the pid.
- **`running` / `stale` / `stopped`** — `status` distinguishes a live process (`running`)
  from a pidfile whose pid is dead (`stale`, file present but process gone — clean it up)
  from no pidfile at all (`stopped`).
- **Graceful stop with escalation** — `stop` sends `SIGTERM`, waits `stop_grace` seconds,
  then escalates to `SIGKILL` if the child is still alive. Always removes the pidfile.
- **No duplicate starts** — `start` raises `AlreadyRunningError` if the pidfile already
  names a live process; a *stale* pidfile is silently reclaimed.
- **Injectable clock + spawner** — the spawner, clock, sleeper, signaller, and liveness
  probe are all parameters. Tests pass a fake process and a fake clock, so the suite runs
  instantly, forks nothing, and asserts the exact backoff schedule.

## Usage

```python
from agenttools_daemon import Supervisor, Backoff

sup = Supervisor(
    ["my-worker", "--serve"],          # argv list (run directly) or a string (run via shell)
    pidfile="/tmp/my-worker.pid",
    backoff=Backoff(base=0.5, factor=2.0, max_delay=30.0),  # or just backoff=0.5
    max_restarts=10,                   # None = unlimited; 0 = run once, never restart
)

sup.start()        # spawn once, write the pidfile (does NOT supervise) -> PidRecord
sup.status()       # -> Status(state="running", pid=..., started_at=..., cmd=...)
sup.restart()      # stop + start, returns the new PidRecord
sup.stop()         # SIGTERM, escalate to SIGKILL after stop_grace, remove pidfile

# Or run the supervision loop in the foreground (e.g. under systemd or a parent process):
sup.run_forever()  # re-spawns the child on every exit with backoff, until cap or stop()
```

`start`/`status`/`stop` are **cross-process**: a second `Supervisor` constructed with the
same `pidfile` (e.g. a `task-cli stop` invocation in a different process) reads the pidfile,
finds the pid, and signals/cleans it — it does not need the original supervisor object.
`run_forever` is the in-process supervision loop and keeps its own handle to the child.

## Backoff schedule

The delay slept **before** the `n`-th (0-based) restart is:

```
delay(n) = min(base * factor ** n, max_delay)
```

With the defaults (`base=0.5`, `factor=2.0`, `max_delay=30.0`), the first restarts sleep
`0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, …` seconds. Backoff is **deterministic — no
jitter**: a single supervised child is not a fleet retrying in lockstep, and a predictable
schedule is easier to reason about and test. (If a fleet ever needs spread, wrap the
`sleeper`.) Pass a bare number as `backoff=` to set just the `base`; pass a `Backoff` for
full control.

## States

`status()` returns a `Status` whose `.state` is one of:

| State | Meaning | What to do |
| --- | --- | --- |
| `running` | pidfile present, recorded pid is alive | nothing |
| `stale` | pidfile present, recorded pid is **dead** (crashed/killed without cleanup, or pid recycled) | clean up / restart |
| `stopped` | no pidfile | nothing is supposed to be running |

`Status.running` is a shorthand for `state == "running"`.

## Restart semantics (`run_forever`)

- The **first spawn is not a restart.** `max_restarts` caps **re-spawns**, so
  `max_restarts=0` runs the child exactly once and returns when it exits;
  `max_restarts=3` spawns up to 4 times total (1 initial + 3 restarts).
- `run_forever` returns the number of restarts it performed.
- A `stop()` (from another thread, a signal handler, or another process via the shared
  pidfile flag) makes the loop exit at the next iteration boundary — including during a
  backoff sleep — rather than spawning again.
- If a child was attached by a prior `start()` and is still alive, `run_forever` adopts it
  instead of spawning a duplicate.

## Stop semantics

`stop()` is idempotent and cross-process:

1. sets the stop flag (so a concurrent `run_forever` exits),
2. reads the pidfile; if absent or the pid is already dead, just clears the file and
   returns `False`,
3. otherwise sends `SIGTERM`, waits up to `stop_grace` seconds, escalates to `SIGKILL` if
   still alive, removes the pidfile, and returns `True`.

A race where the pid exits between the liveness probe and the signal is not an error
(`ProcessLookupError` / `ESRCH` are swallowed).

## Public API

| Symbol | Purpose |
| --- | --- |
| `Supervisor(cmd, *, pidfile, backoff=None, max_restarts=None, stop_grace=5.0, …)` | the supervisor |
| `Supervisor.start() -> PidRecord` | spawn once + write pidfile (no supervision) |
| `Supervisor.stop() -> bool` | SIGTERM→SIGKILL the child, remove pidfile; `True` if one was signalled |
| `Supervisor.status() -> Status` | `running` / `stale` / `stopped` from pidfile + liveness probe |
| `Supervisor.restart() -> PidRecord` | `stop()` then `start()` |
| `Supervisor.run_forever() -> int` | supervision loop; re-spawn on exit with backoff; returns restart count |
| `Backoff(base=0.5, factor=2.0, max_delay=30.0)` | the deterministic backoff value object; `.delay(n)` |
| `Status(state, pid, started_at, cmd)` | a status snapshot; `.running` shorthand |
| `PidRecord(pid, started_at, cmd)` | what the pidfile stores (`.to_json` / `.from_json`) |
| `AlreadyRunningError` | raised by `start()` when a live process is already recorded |
| `default_spawner(cmd) -> Child` | the real `subprocess.Popen` spawner (the default) |

### Constructor parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `cmd` | — | argv list/tuple (run directly) or a string (run via the shell) |
| `pidfile` | — | path to the JSON pidfile |
| `backoff` | `Backoff()` | a `Backoff`, or a bare number used as its `base` |
| `max_restarts` | `None` | cap on re-spawns in one `run_forever` (`None` = unlimited, `0` = run once) |
| `stop_grace` | `5.0` | seconds after `SIGTERM` before escalating to `SIGKILL` |
| `spawner` | `default_spawner` | `Callable[[cmd], Child]` — inject a fake in tests |
| `clock` | `time.monotonic` | `Callable[[], float]` for relative waits (stop grace) |
| `sleeper` | `time.sleep` | `Callable[[float], None]` — inject a recorder in tests |
| `signaller` | `os.kill` | `Callable[[pid, sig], None]` — the cross-process stop signal |
| `alive` | `os.kill(pid, 0)` probe | `Callable[[pid], bool]` liveness check |

## Testability

Every interaction with time and processes is an injected seam, so the supervisor is tested
without a single real long-running process or real sleep:

```python
class FakeChild:
    def __init__(self): self.pid = 4242; self._exits = [0]
    def poll(self): return self._exits.pop(0) if self._exits else 0
    def terminate(self): ...
    def kill(self): ...

delays = []
sup = Supervisor(
    ["worker"], pidfile=tmp / "w.pid",
    max_restarts=2,
    spawner=lambda cmd: FakeChild(),
    sleeper=delays.append,            # records the backoff schedule, sleeps nothing
    alive=lambda pid: False,
)
restarts = sup.run_forever()
assert restarts == 2
assert delays == [0.5, 1.0]           # the exact backoff schedule, asserted
```

See `tests/test_agenttools_daemon.py` for the full suite (restart-on-exit, the backoff
schedule, the `max_restarts` cap, pidfile lifecycle, `stop()` termination/escalation, and
the `running`/`stale`/`stopped` status transitions). The whole suite is HOME-isolated,
deterministic, and touches no network and no real sleeps.

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the `agenttools-daemon`
distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-daemon"]
```

For local/dev installs from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib/agenttools_daemon   # editable install
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_daemon python -c "from agenttools_daemon import Supervisor"
```

## Why stdlib only

The ecosystem is stdlib-first by directive. A focused supervisor is a few hundred lines over
`subprocess` / `os` / `signal` / `time` and adds zero install/import cost. `supervisord` and
`circus` are daemons-of-daemons with their own config formats and control sockets — far more
than `task-cli` / `tg-ctl` need to keep one child alive. Owning it also makes every
time/process interaction an injected seam, which the third-party managers make impossible to
test cleanly.
