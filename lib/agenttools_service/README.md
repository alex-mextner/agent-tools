# agenttools-service

One **reusable service-manager** for every long-running server in the agent-tools ecosystem.
Each daemon — the `review` dashboard, `config-web`, `tg-ctl`, future ones — wants the **same**
lifecycle subcommands. Instead of each tool hand-rolling them (slightly differently, and one
always forgetting the systemd fallback or accidentally launching on the bare command), this is
the single shared copy.

**Stdlib only**, plus one in-ecosystem dependency — [`agenttools-daemon`](../agenttools_daemon)
(also stdlib-only), reused for the pidfile + start/stop/status machinery rather than
reimplemented.

This is the SAME machinery the roadmap wants shared by **tg-ctl autostart** and the
**daemon-supervisor** (§3): one service-management helper, not per-tool copies.

## The lifecycle

| Subcommand | What it does |
| --- | --- |
| `run` | Run in the **foreground** (this shell), blocking. For `disable`d / ad-hoc use. No pidfile (this shell *is* the service). |
| `start` | Start in the **background** (detached daemon); writes a pidfile; returns immediately. |
| `status` | Is it running? Reports `{running, pid, port, url, enabled}`. |
| `stop` | Stop the background instance; remove the pidfile. Idempotent. |
| `enable` | Install OS autostart (launchd / systemd `--user` / a no-systemd fallback) **AND** start it now. Idempotent. |
| `disable` | Remove OS autostart **AND** stop. Idempotent. |
| *(bare)* | No subcommand → print **HELP**, never launch. |

Everything is **idempotent and removable**: re-running `enable` overwrites the unit in place
(no duplicate autostart entries); `disable` is a no-op when nothing is installed/running.

### Single owner — no double-start

`start` (background, pidfile) and `enable` (OS autostart) are two **mutually exclusive**
ownership regimes for the same process, and the manager keeps exactly one owner so two copies
never fight over the port:

- On **launchd / systemd**, installing the unit *also launches the process now* (launchd
  `RunAtLoad`, `systemctl --user enable --now`). So `enable` installs the unit and lets the
  **OS** run it — it does **not** also spawn a pidfile-tracked daemon. `status` then reports
  `running` with `pid=None` and `state="autostart"` (the OS is the supervisor; there is no
  pidfile to read a pid from). `disable` removes the unit, which stops that OS-owned process,
  and *also* stops any manager-spawned (`start`) instance.
- On the **no-autostart fallback** OS, there is no OS to run anything, so `enable` falls back
  to a background `start` (pidfile-tracked) and `status().enabled` stays `False`.
- If the OS control command **fails** (`launchctl`/`systemctl` missing or sandbox-blocked),
  `enable` falls back to a manager-spawned instance so the service is still up, and reports
  `enabled=False` so the caller can warn.

## Quick start

```python
from agenttools_service import Service, ServiceManager

svc = Service(
    name="dashboard",                       # names the pidfile/logfile/autostart unit
    argv=["review", "dashboard", "run"],    # the FOREGROUND server command
    port=7878,                              # for status' url (None for non-network services)
    tool="review",                          # namespaces the autostart label / state dir
    description="review-cli dashboard",
)
mgr = ServiceManager(svc)

mgr.run()       # foreground, blocking -> exit code
mgr.start()     # background detached  -> ServiceStatus(running=True, pid=..., url=...)
mgr.status()    # -> ServiceStatus(running=..., pid=..., port=..., url=..., enabled=...)
mgr.stop()      # -> True if a live process was signalled, else False
mgr.enable()    # install launchd/systemd autostart + start now -> ServiceStatus
mgr.disable()   # remove autostart + stop -> ServiceStatus
```

## Wiring it into a CLI (argparse)

Wire `<tool> <service> run|start|stop|status|enable|disable` in three lines, with the bare
`<tool> <service>` printing help (never launching):

```python
import argparse
from agenttools_service import Service, ServiceManager, add_service_subcommands, dispatch

svc = Service(name="dashboard", argv=["review", "dashboard", "run"], port=7878, tool="review")

parser = argparse.ArgumentParser(prog="review dashboard")
subs = parser.add_subparsers(dest="action")
add_service_subcommands(
    subs,
    manager_factory=lambda: ServiceManager(svc),   # built lazily, only when a subcommand runs
    service_name="dashboard",
)
args = parser.parse_args(argv)
raise SystemExit(dispatch(args, on_no_subcommand=lambda: (parser.print_help() or 0)))
```

`dispatch` returns an exit code: `0` on success, `3` when `status`/`stop` find nothing running
(so a script can branch on "was it up?"). On `start`/`enable` it prints the pid + url and, when
autostart is supported but not yet enabled, hints `run '<tool> <service> enable' to start it at
login`.

## Where files live

Paths honor the XDG base-dir spec (and fall back to the conventional dirs):

| File | Location | Override |
| --- | --- | --- |
| pidfile | `$XDG_STATE_HOME/<tool>/<name>.pid` → `~/.local/state/<tool>/<name>.pid` | `$XDG_STATE_HOME` |
| logfile | `$XDG_CACHE_HOME/<tool>/<name>.log` → `~/.cache/<tool>/<name>.log` | `$XDG_CACHE_HOME` |
| launchd plist | `~/Library/LaunchAgents/com.agenttools.<tool>.<name>.plist` | — |
| systemd unit | `$XDG_CONFIG_HOME/systemd/user/agenttools-<tool>-<name>.service` → `~/.config/...` | `$XDG_CONFIG_HOME` |

The pid + autostart unit are **state**; logs are **cache** — losing a cache dir must not lose
the service's identity.

## Supported-OS matrix

| OS | Autostart backend | `enable` behavior | Who runs the process | Notes |
| --- | --- | --- | --- | --- |
| **macOS** (`darwin`) | **launchd LaunchAgent** | writes `~/Library/LaunchAgents/<label>.plist` (`RunAtLoad` + `KeepAlive{SuccessfulExit:false}`), `launchctl load`s it (which launches it now) | **launchd** (no pidfile) | restarts only on a non-clean exit, not on `exit 0` |
| **Linux** + `systemctl` | **systemd `--user` unit** | writes `…/systemd/user/<unit>.service` (`Restart=on-failure`, `WantedBy=default.target`), `systemctl --user daemon-reload` + `enable --now` (starts it now) | **systemd** (no pidfile) | survives reboot via the user session |
| **Linux**, no `systemctl` | **fallback (no autostart)** | falls back to a background `start` (pidfile), but it will **NOT** survive reboot | **the manager** (pidfile) | `status().enabled` is `False`; the CLI warns |
| **Any other OS** (e.g. `win32`) | **fallback (no autostart)** | same — fallback background `start`, no boot survival | **the manager** (pidfile) | `status().enabled` is `False` |

`run` / `start` / `stop` / `status` work on **every** OS (they only need a subprocess + a
pidfile). Only `enable`/`disable`'s boot-survival half is OS-specific; on an unsupported OS
`enable` still brings the service up now (via the fallback `start`) and reports
`enabled=False` so the caller can warn the user rather than silently pretending autostart was
installed. **Windows caveat:** `start`'s detach (`start_new_session`) is POSIX-only — on
Windows the spawned process is not fully detached from the launching shell. Windows is best-
effort; the supported autostart targets are macOS and Linux.

When autostart owns the process (launchd/systemd) and no manager-spawned pidfile instance is
also running, `status` reports `running=True`, `state="autostart"`, `pid=None` — there is no
pidfile because the OS, not the manager, spawned it. (If a pidfile instance is *also* running,
`state` mirrors the daemon state and `pid` is set.) To stop an autostart-owned service, use
`disable` (which uninstalls the unit and stops it), not `stop` (which only targets a manager-
spawned pidfile instance).

**Honest limitations of the OS-managed path** (deliberately out of scope rather than
half-done):

- `status().running` for an enabled service is inferred from "the unit is installed", not from
  probing the live launchd/systemd process. A service that crashed in a way the OS won't
  auto-restart (a clean `exit 0`, or a `SIGKILL`) can still read `running=True`. For
  authoritative OS-managed liveness use the OS's own tools (`launchctl list`,
  `systemctl --user is-active`).
- Re-running `enable` is **file-idempotent** (no duplicate units) but reloads the unit
  (`launchctl unload`+`load` / `systemctl enable --now`), which **restarts** the running
  daemon. Don't re-run `enable` "just to be sure" on a live service if a restart is costly.
- **launchd PATH:** launchd runs the LaunchAgent with a minimal environment, NOT your shell
  `PATH`. A bare `argv[0]` like `"review"` may not resolve and the agent silently won't start.
  For a reliable LaunchAgent, pass an **absolute** `argv[0]` (e.g. `shutil.which("review")` or
  `sys.executable`) in the `Service`. The plist renderer is pure and does not resolve PATH.

**CLI output conventions** (`run_action` / `dispatch`): success lines go to **stdout**, failure
and warning lines (`already running`, autostart `FAILED`, "won't survive reboot") go to
**stderr**. Exit codes are `0` (ok), `3` (nothing running), `4` (autostart enable/disable
failed) — except `run`, which returns the **raw** exit code of the foreground child (so it may
itself be 3/4); the 3/4 contract applies only to the manager-driven actions.

## Validation & safety

`Service` validates its inputs at construction (fail-closed) because they flow into
filesystem paths, autostart labels, and the rendered launchd/systemd units:

- **`name` / `tool`** must be slugs (`[A-Za-z0-9_.-]`, not `.`/`..`). They become directory and
  unit-file names; a value containing `/` or `..` would write pid/log/plist/unit files outside
  the intended dirs (path traversal).
- **`argv` tokens / `description` / `host`** must contain **no control characters** (newlines,
  tabs, NUL, …). A newline in `description` or an argv token would otherwise inject arbitrary
  directives into a systemd unit (`\n[Service]\nExecStartPre=…`) that the OS runs at every
  login — arbitrary code execution for any consumer that sources these from config. `argv`
  tokens are additionally `%`-escaped and quoted in `ExecStart`.
- **`port`** must be `1..65535` or `None`.

So a consumer can safely pass config-/env-sourced values to `Service` without hand-sanitizing.

## Public API

| Symbol | Purpose |
| --- | --- |
| `Service(name, argv, *, port=None, host="127.0.0.1", tool=None, description="", home=None)` | the service descriptor (paths/label/url derived) |
| `ServiceManager(service, *, platform=…, autostart=None, spawner=…, runner=…, alive=…, signaller=…, foreground_runner=…)` | the lifecycle manager |
| `ServiceManager.run() -> int` | foreground, blocking; returns the exit code |
| `ServiceManager.start() -> ServiceStatus` | background detached; writes pidfile |
| `ServiceManager.status() -> ServiceStatus` | `{running, pid, port, url, enabled}` |
| `ServiceManager.stop() -> bool` | stop background; `True` if one was signalled |
| `ServiceManager.enable() -> ServiceStatus` | install autostart + start now |
| `ServiceManager.disable() -> ServiceStatus` | remove autostart + stop |
| `ServiceManager.autostart_active() -> bool` | unit installed AND the OS command accepted it (same-process) |
| `ServiceManager.last_disable_ok -> bool` | whether the most recent `disable` fully removed autostart |
| `ServiceStatus(running, state, pid, port, url, enabled)` | a status snapshot; `.as_dict()` |
| `add_service_subcommands(subparsers, *, manager_factory, service_name, help_text=None)` | wire the 6 subcommands into argparse |
| `dispatch(args, *, on_no_subcommand) -> int` | run the chosen action; bare → `on_no_subcommand` (HELP) |
| `run_action(manager, action) -> int` | invoke one action, print a one-line result, return an exit code (`0` ok, `3` not-running, `4` autostart enable/disable failed) |
| `select_autostart_backend(*, platform, home, has_systemctl=None, runner=None)` | pick the OS backend |
| `render_launchd_plist(service) -> str` / `render_systemd_unit(service) -> str` | pure unit generators (call with a **validated** `Service`) |
| `LaunchdBackend` / `SystemdUserBackend` / `NoopAutostartBackend` | the concrete backends |
| `AutostartBackend` | the backend `Protocol` (`install` / `uninstall` / `is_installed` / `unit_path` / `kind` / `manages_process`) |
| `current_platform() -> str` | `"darwin"` / `"linux"` / verbatim otherwise |
| `SUBCOMMANDS` | `("run", "start", "stop", "status", "enable", "disable")` |

### `ServiceManager` injectable seams

Every OS/process/path interaction is a parameter, so the whole manager — including
`enable`/`disable` and the launchd/systemd unit generation — is tested under a tmp HOME with no
real autostart and no real process:

| Seam | Default | Purpose |
| --- | --- | --- |
| `platform` | `current_platform()` | which autostart backend to select |
| `has_systemctl` | probe `shutil.which("systemctl")` | Linux backend selection |
| `autostart` | selected by OS | inject an explicit backend (tests) |
| `spawner` | detached `subprocess.Popen` (`start_new_session`, logs → logfile) | how `start` detaches the daemon |
| `runner` | `subprocess.run` (never raises on nonzero) | the launchctl/systemctl control-command runner |
| `alive` | `os.kill(pid, 0)` probe | liveness (passed to the Supervisor) |
| `signaller` | `os.kill` | the cross-process `stop` signal (passed to the Supervisor) |
| `foreground_runner` | `subprocess.call` | how `run` blocks on the foreground process |

## Testability

```python
svc = Service(name="d", argv=["srv"], port=80, tool="t", home=tmp_home)
mgr = ServiceManager(
    svc,
    platform="linux",
    has_systemctl=True,
    spawner=fake_spawner,          # records (argv, logfile), returns a fake Child
    runner=runner_spy,             # records every `systemctl --user …` call, no real systemd
    alive=lambda pid: True,
    signaller=lambda pid, sig: ..., # records the stop signal
)
mgr.enable()
assert ["systemctl", "--user", "enable", "agenttools-t-d.service"] in runner_spy.calls
```

See `tests/test_agenttools_service.py` for the full suite: the descriptor + path resolution,
launchd/systemd **unit generation** (asserted against the rendered text, validated as XML for
the plist), the backend-selection matrix, each backend's install/uninstall/idempotency, the
manager's run/start/status/stop/enable/disable, and the CLI wiring (bare = HELP, never launch).
The whole suite is HOME-isolated and touches no real autostart, no real process, no network.

## Installing / importing as a consumer

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-service"]   # pulls in agenttools-daemon transitively
```

Local/dev install from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib/agenttools_daemon \
            -e /path/to/agent-tools/lib/agenttools_service
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_daemon \
       --with /path/to/agent-tools/lib/agenttools_service \
       python -c "from agenttools_service import Service, ServiceManager"
```

## Why stdlib only (no `python-daemon`, no `launchd`/`systemd` wrappers)

The ecosystem is stdlib-first by directive. Generating a LaunchAgent plist or a systemd unit is
pure text; loading it is one `launchctl`/`systemctl` call. A focused manager over `subprocess` /
`os` / `shutil` adds zero install/import cost and makes every OS/process interaction an injected
seam — which the third-party process managers make impossible to test cleanly. The pidfile +
supervision half is the one place we *do* depend on another package: `agenttools-daemon`, so the
two never drift.
```
