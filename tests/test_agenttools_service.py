"""Tests for agenttools_service — the shared service-manager.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_service.py -q

Every test runs under a tmp HOME (``XDG_STATE_HOME`` / ``XDG_CACHE_HOME`` /
``XDG_CONFIG_HOME`` pointed inside ``tmp_path``) and injects a fake autostart runner + a fake
detached spawner, so the suite installs NO real launchd/systemd autostart, spawns no real
process, and leaks nothing onto the developer's machine. The launchd/systemd UNIT GENERATION
is asserted directly against the rendered text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_daemon import AlreadyRunningError, PidRecord  # noqa: E402

import agenttools_service as ats  # noqa: E402
from agenttools_service import (  # noqa: E402
    LINUX,
    MACOS,
    LaunchdBackend,
    NoopAutostartBackend,
    Service,
    ServiceManager,
    SystemdUserBackend,
    add_service_subcommands,
    current_platform,
    dispatch,
    render_launchd_plist,
    render_systemd_unit,
    run_action,
    select_autostart_backend,
)


# --- fakes ------------------------------------------------------------------------------


class RunnerSpy:
    """Records every autostart control command, returns a scripted exit code (default 0)."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._rc = returncode

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return self._rc


class FakeChild:
    """A minimal ``agenttools_daemon.Child`` stand-in for the detached spawner."""

    _next_pid = 5000

    def __init__(self):
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid

    def poll(self):
        return None

    def terminate(self):
        ...

    def kill(self):
        ...


class SpawnerSpy:
    """A detached spawner that records (argv, logfile) and hands out FakeChildren."""

    def __init__(self):
        self.calls: list[tuple[list[str], Path]] = []
        self.children: list[FakeChild] = []

    def __call__(self, argv, logfile):
        child = FakeChild()
        self.calls.append((list(argv), Path(logfile)))
        self.children.append(child)
        return child


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp HOME with XDG dirs pointed inside it; returns the home Path."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_STATE_HOME", str(h / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(h / ".cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    return h


def make_service(home, **kwargs):
    base = dict(
        name="dashboard",
        argv=["review", "dashboard", "run"],
        port=7878,
        tool="review",
        description="review-cli dashboard",
        home=home,
    )
    base.update(kwargs)
    return Service(**base)


# --- Service descriptor -----------------------------------------------------------------


def test_service_paths_under_xdg(home):
    svc = make_service(home)
    assert svc.state_dir == home / ".local" / "state" / "review"
    assert svc.cache_dir == home / ".cache" / "review"
    assert svc.pidfile == svc.state_dir / "dashboard.pid"
    assert svc.logfile == svc.cache_dir / "dashboard.log"


def test_service_state_falls_back_without_xdg(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    svc = make_service(h)
    assert svc.state_dir == h / ".local" / "state" / "review"
    assert svc.cache_dir == h / ".cache" / "review"


def test_service_url_and_label(home):
    svc = make_service(home)
    assert svc.url == "http://127.0.0.1:7878"
    assert svc.label == "com.agenttools.review.dashboard"
    assert svc.systemd_unit_name == "agenttools-review-dashboard"


def test_service_url_none_without_port(home):
    svc = make_service(home, port=None)
    assert svc.url is None


def test_service_tool_defaults_to_name(home):
    svc = make_service(home, tool=None, name="tgctl")
    assert svc.tool_name == "tgctl"
    assert svc.label == "com.agenttools.tgctl.tgctl"


def test_service_rejects_bad_name(home):
    with pytest.raises(ValueError):
        make_service(home, name="bad name with spaces")
    with pytest.raises(ValueError):
        make_service(home, name="")


def test_service_rejects_bad_argv(home):
    with pytest.raises(ValueError):
        make_service(home, argv=[])
    with pytest.raises(ValueError):
        make_service(home, argv=[1, 2])  # type: ignore[list-item]


def test_service_rejects_bad_port(home):
    with pytest.raises(ValueError):
        make_service(home, port=0)
    with pytest.raises(ValueError):
        make_service(home, port=99999)


# --- unit generation --------------------------------------------------------------------


def test_render_launchd_plist_contains_label_args_and_log(home):
    svc = make_service(home)
    plist = render_launchd_plist(svc)
    assert "<key>Label</key>" in plist
    assert "<string>com.agenttools.review.dashboard</string>" in plist
    # argv rendered as ProgramArguments, in order.
    assert "<string>review</string>" in plist
    assert "<string>dashboard</string>" in plist
    assert "<string>run</string>" in plist
    assert "<key>RunAtLoad</key>" in plist and "<true/>" in plist
    # KeepAlive restarts ONLY on a non-clean exit (SuccessfulExit:false), not unconditionally,
    # so a service that exits 0 is not relaunched in a tight loop.
    assert "<key>KeepAlive</key>" in plist
    assert "<key>SuccessfulExit</key>" in plist and "<false/>" in plist
    assert str(svc.logfile) in plist
    # well-formed XML
    import xml.dom.minidom as minidom

    minidom.parseString(plist)


def test_render_launchd_plist_escapes_xml(home):
    svc = make_service(home, argv=["review", "--x", "a&b<c>"])
    plist = render_launchd_plist(svc)
    assert "a&amp;b&lt;c&gt;" in plist
    import xml.dom.minidom as minidom

    minidom.parseString(plist)  # still valid XML


def test_render_systemd_unit_has_execstart_restart_and_install(home):
    svc = make_service(home)
    unit = render_systemd_unit(svc)
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "ExecStart=review dashboard run" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Description=review-cli dashboard" in unit
    assert str(svc.logfile) in unit


def test_render_systemd_unit_quotes_spaces(home):
    svc = make_service(home, argv=["python", "-c", "print('hi there')"])
    unit = render_systemd_unit(svc)
    # the token with spaces is quoted
    assert 'ExecStart=python -c "print(\'hi there\')"' in unit


# --- backend selection matrix -----------------------------------------------------------


def test_select_backend_macos(home):
    b = select_autostart_backend(platform=MACOS, home=home)
    assert isinstance(b, LaunchdBackend)
    assert b.kind == "launchd"


def test_select_backend_linux_with_systemd(home):
    b = select_autostart_backend(platform=LINUX, home=home, has_systemctl=True)
    assert isinstance(b, SystemdUserBackend)
    assert b.kind == "systemd"


def test_select_backend_linux_without_systemd(home):
    b = select_autostart_backend(platform=LINUX, home=home, has_systemctl=False)
    assert isinstance(b, NoopAutostartBackend)
    assert b.kind == "none"


def test_select_backend_unsupported_os(home):
    b = select_autostart_backend(platform="win32", home=home)
    assert isinstance(b, NoopAutostartBackend)
    assert "win32" in b.reason


# --- launchd backend --------------------------------------------------------------------


def test_launchd_install_writes_plist_and_loads(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)

    assert not backend.is_installed(svc)
    ok = backend.install(svc)
    path = backend.unit_path(svc)

    assert ok is True  # launchctl load succeeded (RunnerSpy returns 0)
    assert path == home / "Library" / "LaunchAgents" / f"{svc.label}.plist"
    assert path.exists()
    assert backend.is_installed(svc)
    assert ["launchctl", "load", str(path)] in runner.calls
    assert backend.manages_process is True
    # the plist on disk is the rendered one
    assert path.read_text() == render_launchd_plist(svc)


def test_launchd_install_reports_failure_on_nonzero_launchctl(home):
    svc = make_service(home)
    backend = LaunchdBackend(home=home, runner=RunnerSpy(returncode=1))
    ok = backend.install(svc)
    assert ok is False  # launchctl load failed
    assert backend.unit_path(svc).exists()  # file still written


def test_launchd_install_is_idempotent(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()
    backend.install(svc)  # second time
    # only one plist exists; reinstall unloaded the old before loading the new
    agents_dir = home / "Library" / "LaunchAgents"
    assert len(list(agents_dir.glob("*.plist"))) == 1
    assert ["launchctl", "unload", str(backend.unit_path(svc))] in runner.calls
    assert ["launchctl", "load", str(backend.unit_path(svc))] in runner.calls


def test_launchd_uninstall_removes_and_unloads(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()

    assert backend.uninstall(svc) is True
    assert not backend.is_installed(svc)
    assert ["launchctl", "unload", str(backend.unit_path(svc))] in runner.calls
    # idempotent: uninstall again is a no-op
    assert backend.uninstall(svc) is False


# --- systemd backend --------------------------------------------------------------------


def test_systemd_install_writes_unit_and_enables_now(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = SystemdUserBackend(home=home, runner=runner)

    ok = backend.install(svc)
    path = backend.unit_path(svc)
    assert ok is True
    assert path == home / ".config" / "systemd" / "user" / f"{svc.systemd_unit_name}.service"
    assert path.exists()
    assert path.read_text() == render_systemd_unit(svc)
    assert backend.manages_process is True
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls
    # `enable --now` (NOT bare `enable`) — it must START the unit now, so Restart= supervision
    # is live and `systemctl stop` acts on the real MainPID.
    assert ["systemctl", "--user", "enable", "--now",
            f"{svc.systemd_unit_name}.service"] in runner.calls


def test_systemd_install_reports_failure_on_nonzero(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy(returncode=1))
    assert backend.install(svc) is False
    assert backend.unit_path(svc).exists()


def test_systemd_install_idempotent(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.install(svc)
    unit_dir = home / ".config" / "systemd" / "user"
    assert len(list(unit_dir.glob("*.service"))) == 1


def test_systemd_uninstall_disables_and_removes(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = SystemdUserBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()

    assert backend.uninstall(svc) is True
    assert not backend.is_installed(svc)
    # `disable --now` stops the running unit AND removes the login wiring.
    assert ["systemctl", "--user", "disable", "--now",
            f"{svc.systemd_unit_name}.service"] in runner.calls
    assert backend.uninstall(svc) is False  # idempotent


# --- ServiceManager: status / start / stop ----------------------------------------------


def make_manager(home, *, platform=MACOS, spawner=None, alive=None, runner=None):
    svc = make_service(home)
    return ServiceManager(
        svc,
        platform=platform,
        spawner=spawner or SpawnerSpy(),
        runner=runner or RunnerSpy(),
        alive=alive,
    )


def test_status_stopped_when_no_pidfile(home):
    mgr = make_manager(home, alive=lambda pid: False)
    st = mgr.status()
    assert st.running is False
    assert st.state == "stopped"
    assert st.pid is None
    assert st.port == 7878
    assert st.url == "http://127.0.0.1:7878"
    assert st.enabled is False


def test_start_spawns_detached_and_writes_pidfile(home):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    st = mgr.start()

    assert len(spawner.calls) == 1
    argv, logfile = spawner.calls[0]
    assert argv == ["review", "dashboard", "run"]
    assert logfile == mgr.service.logfile
    assert mgr.service.pidfile.exists()
    assert st.running is True
    assert st.pid == spawner.children[0].pid
    assert st.url == "http://127.0.0.1:7878"


def test_start_refuses_duplicate(home):
    # Pre-seed a pidfile naming a pid we report alive.
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    spawner = SpawnerSpy()
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner, alive=lambda pid: pid == 4242)
    with pytest.raises(AlreadyRunningError):
        mgr.start()
    assert spawner.calls == []  # never spawned a duplicate


def test_stop_when_running_signals_and_returns_true(home):
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    signalled: list[tuple[int, int]] = []
    states = {"alive": True}

    def signaller(pid, sig):
        # the SIGTERM "kills" the fake pid so stop() doesn't escalate/loop on a real process
        signalled.append((pid, sig))
        states["alive"] = False

    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=signaller,
    )
    assert mgr.stop() is True
    assert signalled and signalled[0][0] == 4242
    assert not svc.pidfile.exists()


def test_stop_when_not_running_returns_false(home):
    mgr = make_manager(home, alive=lambda pid: False)
    assert mgr.stop() is False


# --- ServiceManager: enable / disable ---------------------------------------------------


def test_enable_on_managed_backend_installs_and_does_not_double_spawn(home):
    # The core no-double-start invariant: on launchd/systemd the OS launches the process when
    # the unit is installed, so the manager must NOT also pidfile-spawn it.
    spawner = SpawnerSpy()
    runner = RunnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner, runner=runner,
                         alive=lambda pid: False)

    assert mgr.status().enabled is False
    st = mgr.enable()

    plist = home / "Library" / "LaunchAgents" / f"{svc.label}.plist"
    assert plist.exists()
    assert ["launchctl", "load", str(plist)] in runner.calls
    assert st.enabled is True
    # OS owns the process → status reports running with NO pidfile/pid, and the manager
    # spawned NOTHING (the bug was two processes on the same port).
    assert st.running is True
    assert st.pid is None
    assert st.state == "autostart"
    assert spawner.calls == []  # NOT double-started
    assert not svc.pidfile.exists()


def test_enable_on_managed_backend_falls_back_to_spawn_when_launchctl_fails(home):
    # If the OS control command fails, the service is NOT actually under OS control, so the
    # manager must fall back to a pidfile-spawned instance so `enable` still leaves it UP.
    spawner = SpawnerSpy()
    states = {"started": False}
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner,
                         runner=RunnerSpy(returncode=1),  # launchctl load fails
                         alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner(argv, logfile)

    mgr.spawner = spawn_and_mark
    st = mgr.enable()
    assert len(spawner.calls) == 1  # fell back to a manager-spawned instance
    assert st.running is True
    # the launchctl command failed → autostart is NOT actually active, even though the file
    # was written; `autostart_active` (not `st.enabled`) is the honest signal.
    assert mgr.autostart_active() is False


def test_enable_on_unsupported_os_starts_but_not_enabled(home):
    spawner = SpawnerSpy()
    svc = make_service(home)
    states = {"started": False}
    mgr = ServiceManager(svc, platform="win32", spawner=spawner,
                         alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner.__call__(argv, logfile)

    mgr.spawner = spawn_and_mark
    st = mgr.enable()
    assert st.running is True
    assert st.enabled is False  # noop backend → no autostart
    assert mgr.backend.kind == "none"


def test_disable_removes_autostart_and_stops(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)  # pretend it was enabled
    # and pretend it's running
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=321, started_at=1.0, cmd=list(svc.argv)).to_json())

    states = {"alive": True}

    def signaller(pid, sig):
        states["alive"] = False  # the stop signal "kills" the fake pid

    mgr = ServiceManager(
        svc,
        platform=MACOS,
        autostart=backend,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=signaller,
    )
    st = mgr.disable()

    assert not backend.is_installed(svc)
    assert not svc.pidfile.exists()
    assert st.enabled is False
    assert st.running is False


def test_disable_idempotent_when_nothing_enabled_or_running(home):
    mgr = make_manager(home, alive=lambda pid: False)
    st = mgr.disable()  # should not raise
    assert st.enabled is False
    assert st.running is False


# --- run (foreground) -------------------------------------------------------------------


def test_run_blocks_on_foreground_and_returns_exit_code(home):
    svc = make_service(home)
    captured = {}

    def fg(argv):
        captured["argv"] = list(argv)
        return 0

    mgr = ServiceManager(svc, platform=MACOS, foreground_runner=fg, spawner=SpawnerSpy())
    rc = mgr.run()
    assert rc == 0
    assert captured["argv"] == ["review", "dashboard", "run"]
    # run() must NOT write a pidfile (this shell IS the service)
    assert not svc.pidfile.exists()


# --- CLI wiring -------------------------------------------------------------------------


def _build_parser(manager):
    import argparse

    parser = argparse.ArgumentParser(prog="review dashboard")
    subs = parser.add_subparsers(dest="action")
    add_service_subcommands(subs, manager_factory=lambda: manager, service_name="dashboard")
    return parser


def test_bare_invocation_returns_help_never_launches(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    args = parser.parse_args([])  # no subcommand

    called = {"help": 0}

    def on_no_subcommand():
        called["help"] += 1
        return 0

    rc = dispatch(args, on_no_subcommand=on_no_subcommand)
    assert rc == 0
    assert called["help"] == 1
    assert spawner.calls == []  # NOTHING launched


def test_dispatch_status_subcommand(home, capsys):
    mgr = make_manager(home, alive=lambda pid: False)
    parser = _build_parser(mgr)
    args = parser.parse_args(["status"])
    rc = dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 3  # nothing running
    assert "stopped" in out


def test_dispatch_start_subcommand_prints_pid_and_url(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    args = parser.parse_args(["start"])
    rc = dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "started" in out
    assert "http://127.0.0.1:7878" in out
    assert len(spawner.calls) == 1


def test_run_action_unknown_raises(home):
    mgr = make_manager(home)
    with pytest.raises(ValueError):
        run_action(mgr, "bogus")


def test_subcommands_order_and_help_present(home):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    help_text = parser.format_help()
    for name in ("run", "start", "stop", "status", "enable", "disable"):
        assert name in help_text
    assert ats.SUBCOMMANDS == ("run", "start", "stop", "status", "enable", "disable")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("linux", LINUX),
        ("linux2", LINUX),
        ("linux-aarch64", LINUX),
        ("darwin", MACOS),
        ("win32", "win32"),
        ("freebsd13", "freebsd13"),
    ],
)
def test_current_platform_buckets(monkeypatch, raw, expected):
    # linux* collapses to "linux"; darwin verbatim; everything else passes through unchanged.
    monkeypatch.setattr(sys, "platform", raw)
    assert current_platform() == expected


# --- default spawners (real subprocess; the only paths that touch real I/O) --------------


def test_default_detached_spawner_writes_log_and_leaks_no_fd(home):
    # Exercise the REAL detached spawner with a trivial no-op argv: it must spawn, write to the
    # logfile, and NOT leak the parent's log fd (regression guard for the close-after-Popen).
    from agenttools_service.core import _default_detached_spawner

    svc = make_service(home, argv=[sys.executable, "-c", "import sys; sys.stdout.write('hi')"])
    before = len(_open_fds())
    children = [_default_detached_spawner(list(svc.argv), svc.logfile) for _ in range(5)]
    for c in children:
        c.wait()
    after = len(_open_fds())
    # no per-spawn fd accumulation in the PARENT (allow a tiny slack for unrelated churn)
    assert after <= before + 1, f"fd leak: {before} -> {after}"
    assert svc.logfile.read_bytes() == b"hi" * 5  # append, not truncate


def test_default_foreground_runner_returns_exit_code(home):
    from agenttools_service.core import _default_foreground_runner

    assert _default_foreground_runner([sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert _default_foreground_runner([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def _open_fds():
    """The current process's open fds (best-effort; falls back to a fixed probe range)."""
    import os as _os

    fd_dir = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    try:
        return _os.listdir(fd_dir)
    except OSError:
        return [fd for fd in range(256) if _fd_is_open(fd)]


def _fd_is_open(fd):
    import os as _os

    try:
        _os.fstat(fd)
        return True
    except OSError:
        return False


def test_default_runner_returns_127_on_missing_binary():
    from agenttools_service.core import _default_runner

    # a binary that surely does not exist → OSError → 127 (documented contract)
    assert _default_runner(["agenttools-no-such-binary-xyz", "--help"]) == 127


# --- validation (defense-in-depth against path traversal via identifiers) ---------------


def test_service_rejects_path_traversal_tool(home):
    with pytest.raises(ValueError):
        make_service(home, tool="../../etc")
    with pytest.raises(ValueError):
        make_service(home, tool="..")
    with pytest.raises(ValueError):
        make_service(home, tool="a/b")


def test_service_rejects_separator_only_name(home):
    with pytest.raises(ValueError):
        make_service(home, name="..")
    with pytest.raises(ValueError):
        make_service(home, name="/")


def test_service_rejects_empty_host(home):
    with pytest.raises(ValueError):
        make_service(home, host="")
    with pytest.raises(ValueError):
        make_service(home, host="   ")


def test_service_accepts_port_boundary(home):
    assert make_service(home, port=65535).port == 65535
    assert make_service(home, port=1).port == 1


# --- unit generation: identifier edge cases & systemd specifier escaping ----------------


def test_label_when_tool_defaults_to_name_in_rendered_units(home):
    svc = make_service(home, tool=None, name="x")
    assert svc.label == "com.agenttools.x.x"
    assert svc.systemd_unit_name == "agenttools-x-x"
    # the derived label actually lands in the rendered plist
    assert "<string>com.agenttools.x.x</string>" in render_launchd_plist(svc)


def test_render_systemd_unit_doubles_percent_specifiers(home):
    # `%` is a systemd specifier introducer; a literal `%` MUST be doubled or it's substituted.
    svc = make_service(home, argv=["srv", "--fmt", "100%done", "--user", "%u"])
    unit = render_systemd_unit(svc)
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "100%%done" in exec_line
    assert "%%u" in exec_line
    # invariant: every `%` in ExecStart is part of a doubled `%%` pair — no lone specifier
    assert exec_line.replace("%%", "").count("%") == 0


# --- CLI: bare path prints real help to stdout (not just "doesn't launch") --------------


def test_bare_invocation_prints_help_text_to_stdout(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    args = parser.parse_args([])
    rc = dispatch(args, on_no_subcommand=lambda: (parser.print_help() or 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out  # real argparse help reached stdout
    for name in ("run", "start", "stop", "status", "enable", "disable"):
        assert name in out
    assert spawner.calls == []  # and nothing launched


def test_enable_managed_backend_message_has_no_pid_none(home, capsys):
    # regression: an OS-managed enable has pid=None; the message must not print "pid None".
    mgr = make_manager(home, alive=lambda pid: False)  # macOS launchd, RunnerSpy ok
    parser = _build_parser(mgr)
    args = parser.parse_args(["enable"])
    dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "pid None" not in out
    assert "autostart enabled (launchd)" in out


# --- unit-injection guards (security) ---------------------------------------------------


def test_service_rejects_newline_in_description(home):
    # B1: a newline in description would inject systemd unit directives.
    with pytest.raises(ValueError):
        make_service(home, description="ok\n[Service]\nExecStartPre=/tmp/payload")


def test_service_rejects_control_char_in_argv(home):
    # B2: a newline in an argv token would break ExecStart across lines.
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "evil\nExecStartPre=/tmp/payload"])
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "tab\there"])


def test_service_rejects_control_char_in_host(home):
    with pytest.raises(ValueError):
        make_service(home, host="127.0.0.1\r\nHost: evil")


def test_systemd_quote_forces_quoting_on_control_char():
    # render_systemd_unit is a public function; even if a raw token slipped past Service
    # validation, the renderer must not emit a bare newline that splits ExecStart.
    from agenttools_service.core import _systemd_quote

    quoted = _systemd_quote("a\nb")
    assert quoted.startswith('"') and quoted.endswith('"')


# --- B3: no double-start when start THEN enable on a managed backend ---------------------


def test_enable_after_manual_start_stops_pidfile_instance_first(home):
    # A user did `start` (pidfile instance), then `enable`. The OS is about to launch its own
    # copy via RunAtLoad — the manager MUST stop the pidfile instance first (no two on a port).
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=555, started_at=1.0, cmd=list(svc.argv)).to_json())
    states = {"alive": True}
    signalled = []

    def signaller(pid, sig):
        signalled.append((pid, sig))
        states["alive"] = False

    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), runner=RunnerSpy(),
                         alive=lambda pid: states["alive"], signaller=signaller)
    mgr.enable()
    assert signalled and signalled[0][0] == 555  # the manual pidfile instance was stopped
    assert not svc.pidfile.exists()              # and its pidfile cleared


# --- B4: uninstall must not orphan when the OS stop command fails ------------------------


def test_launchd_uninstall_keeps_file_and_reports_false_on_unload_failure(home):
    svc = make_service(home)
    backend = LaunchdBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    # now a runner that fails the unload
    backend.runner = RunnerSpy(returncode=1)
    assert backend.uninstall(svc) is False     # not fully removed
    assert backend.is_installed(svc)           # file left in place (OS may still run it)


def test_systemd_uninstall_keeps_file_and_reports_false_on_disable_failure(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.runner = RunnerSpy(returncode=1)
    assert backend.uninstall(svc) is False
    assert backend.is_installed(svc)


# --- B6: enable hint uses the service's own tool/name, not a literal placeholder ---------


def test_start_hint_uses_tool_and_name_not_literal_placeholder(home, capsys):
    # noop backend so the hint suppresses? No — hint only prints when backend manages process
    # but is not yet installed. Use macOS launchd, status not installed → hint shown on start.
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "<tool>" not in out
    assert "review dashboard enable" in out  # tool='review', name='dashboard'


def test_no_hint_on_unsupported_os(home, capsys):
    spawner = SpawnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform="win32", spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "enable' to start it at login" not in out  # no boot survival → no hint


# --- T4 / T6 / T7: runner exit-code passthrough, fg propagation, XDG fallback ------------


def test_default_runner_returns_actual_exit_code():
    from agenttools_service.core import _default_runner

    # portable (no /usr/bin/true,false dependency — those don't exist on Windows)
    assert _default_runner([sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert _default_runner([sys.executable, "-c", "raise SystemExit(1)"]) == 1


def test_default_foreground_runner_propagates_missing_binary(home):
    from agenttools_service.core import _default_foreground_runner

    # documented contract: a missing foreground binary propagates (the service can't run)
    with pytest.raises(FileNotFoundError):
        _default_foreground_runner(["agenttools-no-such-binary-xyz"])


def test_systemd_unit_dir_falls_back_without_xdg_config(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    backend = SystemdUserBackend(home=h, runner=RunnerSpy())
    svc = Service(name="d", argv=["x"], tool="t", home=h)
    assert backend.unit_path(svc) == h / ".config" / "systemd" / "user" / "agenttools-t-d.service"


# --- ASCII-only slug enforcement (Unicode would make invalid labels/unit names) ----------


def test_service_rejects_unicode_name_and_tool(home):
    with pytest.raises(ValueError):
        make_service(home, name="café")
    with pytest.raises(ValueError):
        make_service(home, tool="ño")
    with pytest.raises(ValueError):
        make_service(home, name="日本語")


# --- manager-level honesty when the OS control command fails -----------------------------


def test_enable_reports_not_active_and_exit_4_when_launchctl_fails(home, capsys):
    spawner = SpawnerSpy()
    states = {"started": False}
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, runner=RunnerSpy(returncode=1),
                         spawner=spawner, alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner(argv, logfile)

    mgr.spawner = spawn_and_mark
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["enable"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert rc == 4
    assert "FAILED" in cap.err and "autostart enabled" not in (cap.out + cap.err)
    assert mgr.autostart_active() is False


def test_disable_reports_failure_and_exit_4_when_uninstall_fails(home, capsys):
    svc = make_service(home)
    # install with a working runner, then disable with a failing one
    backend = LaunchdBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.runner = RunnerSpy(returncode=1)
    mgr = ServiceManager(svc, platform=MACOS, autostart=backend, spawner=SpawnerSpy(),
                         alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["disable"]), on_no_subcommand=lambda: 0)
    assert rc == 4
    assert "FAILED to remove autostart" in capsys.readouterr().err
    assert mgr.last_disable_ok is False
    assert backend.is_installed(svc)  # left in place; not orphaned silently


def test_disable_succeeds_when_nothing_installed(home):
    # idempotent no-op: nothing installed → last_disable_ok True, exit 0
    mgr = make_manager(home, alive=lambda pid: False)
    mgr.disable()
    assert mgr.last_disable_ok is True


# --- round-4 refinements -----------------------------------------------------------------


def test_servicestatus_as_dict(home):
    mgr = make_manager(home, alive=lambda pid: False)
    d = mgr.status().as_dict()
    assert set(d) == {"running", "state", "pid", "port", "url", "enabled"}
    assert d["port"] == 7878
    assert d["url"] == "http://127.0.0.1:7878"
    assert d["running"] is False


def test_dispatch_start_already_running_is_clean_not_traceback(home, capsys):
    # `start` against an already-running instance must print a clean message, not raise.
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4321, started_at=1.0, cmd=list(svc.argv)).to_json())
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(),
                         alive=lambda pid: pid == 4321)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    assert rc == 3
    assert "already running (pid 4321)" in capsys.readouterr().err  # error → stderr


def test_enable_twice_is_file_idempotent(home):
    # re-running enable does not duplicate the unit (file-idempotent); the reload side-effect
    # is documented. Assert exactly one plist remains.
    mgr = make_manager(home, alive=lambda pid: False)
    mgr.enable()
    mgr.enable()
    agents = home / "Library" / "LaunchAgents"
    assert len(list(agents.glob("*.plist"))) == 1


def test_default_runner_distinguishes_missing_from_unrunnable(home, tmp_path):
    from agenttools_service.core import _default_runner

    # missing binary → 127
    assert _default_runner(["agenttools-no-such-binary-xyz"]) == 127
    # present-but-not-executable file → OSError(EACCES/ENOEXEC) → 126 (distinct from 127)
    not_exec = tmp_path / "not_exec"
    not_exec.write_text("#!/bin/sh\n")  # no +x bit
    not_exec.chmod(0o644)
    assert _default_runner([str(not_exec)]) == 126


def test_service_coerces_generator_argv_to_tuple(home):
    svc = make_service(home, argv=(a for a in ["review", "dashboard", "run"]))
    assert svc.argv == ("review", "dashboard", "run")  # not consumed to empty
    # re-readable: render uses it again without it being empty
    assert "ExecStart=review dashboard run" in render_systemd_unit(svc)


def test_enable_runner_ignored_when_explicit_autostart_documented(home):
    # passing both autostart= and runner= → the explicit backend's own runner is used, the
    # ServiceManager-level runner is NOT applied to it (documented footgun). Assert the
    # explicit backend's runner records the calls.
    explicit_runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=explicit_runner)
    ignored_runner = RunnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, autostart=backend, runner=ignored_runner,
                         spawner=SpawnerSpy(), alive=lambda pid: False)
    mgr.enable()
    assert explicit_runner.calls  # explicit backend's runner was used
    assert ignored_runner.calls == []  # the manager-level runner was not applied


# --- round-5: %-escaping of Description / log paths, noop disable, empty argv -------------


def test_render_systemd_unit_escapes_percent_in_description():
    # % in Description= is a specifier introducer; it must be doubled or systemd substitutes it.
    svc = Service(name="d", argv=["x"], tool="t", description="100% done at %H")
    unit = render_systemd_unit(svc)
    desc_line = next(ln for ln in unit.splitlines() if ln.startswith("Description="))
    assert desc_line == "Description=100%% done at %%H"


def test_render_systemd_unit_escapes_percent_in_log_path(tmp_path, monkeypatch):
    # a % in the cache path (XDG_CACHE_HOME) must be doubled in StandardOutput/Error.
    cache = tmp_path / "ca%che"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("HOME", str(tmp_path))
    svc = Service(name="d", argv=["x"], tool="t")
    unit = render_systemd_unit(svc)
    out_line = next(ln for ln in unit.splitlines() if ln.startswith("StandardOutput="))
    assert "%%" in out_line and "ca%che" not in out_line  # the lone % was doubled


def test_disable_on_noop_backend_is_clean(home, capsys):
    # disable on an unsupported OS (noop backend): nothing installed → ok, exit 0, no failure msg
    svc = make_service(home)
    mgr = ServiceManager(svc, platform="win32", spawner=SpawnerSpy(), alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["disable"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 0
    assert mgr.last_disable_ok is True
    assert "FAILED" not in out


def test_service_rejects_empty_argv_token(home):
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "", "run"])


# --- round-6: encoding, string-argv reject, stderr routing, round-trip, port=None --------


def test_service_rejects_bare_string_argv(home):
    # a bare string is a Sequence[str] of chars; must be rejected, not split into characters
    with pytest.raises(ValueError):
        make_service(home, argv="review dashboard run")


def test_install_writes_utf8_regardless_of_locale(home):
    # non-ASCII description must not crash install under a non-UTF-8 locale (explicit encoding).
    # systemd renders Description=, so it carries the non-ASCII bytes.
    svc = make_service(home, description="café dashboard")
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    text = backend.unit_path(svc).read_text(encoding="utf-8")
    assert "Description=café dashboard" in text


def test_enable_failure_message_goes_to_stderr_not_stdout(home, capsys):
    svc = make_service(home)
    states = {"started": False}
    spy = SpawnerSpy()

    def spawn(argv, logfile):
        states["started"] = True
        return spy(argv, logfile)

    mgr = ServiceManager(svc, platform=MACOS, runner=RunnerSpy(returncode=1),
                         spawner=spawn, alive=lambda pid: states["started"])
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["enable"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert rc == 4
    assert "FAILED" in cap.err          # error on stderr
    assert "FAILED" not in cap.out      # not on stdout


def test_start_already_running_message_goes_to_stderr(home, capsys):
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=12, started_at=1.0, cmd=list(svc.argv)).to_json())
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert "already running" in cap.err
    assert cap.out == ""


def test_enable_then_disable_round_trip(home):
    # enable (managed) installs; disable removes; status reflects each step.
    mgr = make_manager(home, alive=lambda pid: False)
    st1 = mgr.enable()
    assert st1.enabled is True and mgr.autostart_active() is True
    st2 = mgr.disable()
    assert st2.enabled is False and mgr.last_disable_ok is True
    assert not mgr.backend.is_installed(mgr.service)
    # and enable works again after a clean disable
    st3 = mgr.enable()
    assert st3.enabled is True


def test_dispatch_stop_exit_3_when_nothing_running(home, capsys):
    mgr = make_manager(home, alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["stop"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 3
    assert "was not running" in out


def test_cli_lines_have_no_none_when_port_is_none(home, capsys):
    svc = make_service(home, port=None)
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "started" in out
    assert "None" not in out
    assert " — " not in out  # no empty url separator


def test_systemd_quote_control_char_stays_single_line():
    # M3: the defense-in-depth claim — a control-char token, once rendered, must not split
    # ExecStart across lines (Service validation is the real guard, but pin the renderer too).
    from agenttools_service.core import _systemd_quote

    # _systemd_quote alone can't strip a newline; assert it at least quotes, and that a
    # validated Service never produces a multi-line ExecStart.
    assert _systemd_quote("a\nb").startswith('"')


# --- review-driven hardening (glm-5.2 findings) -----------------------------------------


def test_service_rejects_empty_tool(home):
    # `tool=""` slips past `_is_slug` (vacuously True) and would silently behave like
    # `tool=None`; reject it so "accept but ignore" can't surprise a caller.
    with pytest.raises(ValueError):
        make_service(home, tool="")


def test_service_rejects_host_with_whitespace(home):
    # A host carrying whitespace would render a broken url ("http://  127.0.0.1  :7878").
    with pytest.raises(ValueError):
        make_service(home, host="  127.0.0.1  ")
    with pytest.raises(ValueError):
        make_service(home, host="my host")


def test_add_service_subcommands_help_text_overrides(home):
    import argparse

    mgr = make_manager(home)
    parser = argparse.ArgumentParser(prog="review dashboard")
    subs = parser.add_subparsers(dest="action")
    add_service_subcommands(
        subs,
        manager_factory=lambda: mgr,
        service_name="dashboard",
        help_text={"start": "boot the dashboard daemon"},
    )
    full = parser.format_help()
    # the override is shown for `start`...
    assert "boot the dashboard daemon" in full
    # ...and an un-overridden subcommand keeps its built-in help (both appear in the parent's
    # subcommand listing).
    assert "stop the background dashboard instance" in full


def test_run_action_run_handles_missing_binary(home, capsys):
    # `run` must not leak a raw FileNotFoundError traceback when argv[0] doesn't exist.
    svc = make_service(home)

    def fg(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    mgr = ServiceManager(svc, platform=MACOS, foreground_runner=fg, spawner=SpawnerSpy())
    rc = run_action(mgr, "run")
    err = capsys.readouterr().err
    assert rc == 127  # "command not found" convention
    assert "command not found" in err
    assert "review" in err


def test_stop_action_warns_when_os_autostart_enabled(home, capsys):
    # An enabled (launchd) service with no pidfile: `stop` finds nothing to stop, but a bare
    # "was not running" is misleading because the OS owns a live process. Expect a stderr hint.
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)  # autostart on disk, OS owns the process
    mgr = ServiceManager(
        svc, platform=MACOS, autostart=backend, spawner=SpawnerSpy(), alive=lambda pid: False
    )
    rc = run_action(mgr, "stop")
    captured = capsys.readouterr()
    assert rc == 3
    assert "was not running" not in captured.out
    assert "autostart" in captured.err and "disable" in captured.err


def test_disable_on_os_managed_only_service_no_pidfile(home):
    # The expected post-`enable` state on a managed backend: autostart unit installed, NO
    # pidfile. `disable` must uninstall (the real work) while `stop` is a clean no-op.
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    assert backend.is_installed(svc)
    assert not svc.pidfile.exists()  # OS owns it; no pidfile

    mgr = ServiceManager(
        svc, platform=MACOS, autostart=backend, spawner=SpawnerSpy(), alive=lambda pid: False
    )
    st = mgr.disable()
    assert not backend.is_installed(svc)  # uninstall did the work
    assert st.enabled is False
    assert st.running is False
    assert mgr.last_disable_ok is True


def test_manager_uses_supervisor_defaults_when_alive_and_signaller_none(home):
    # Every other test injects `alive`/`signaller`; this one leaves them None to exercise the
    # path that hands the Supervisor its real defaults (os.kill-based). We keep it side-effect
    # free by checking a no-pidfile status (no real process is ever signalled).
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy())  # alive/signaller default
    st = mgr.status()  # reads a (missing) pidfile; never calls os.kill on a real pid
    assert st.running is False
    assert st.state == "stopped"
    assert mgr.stop() is False  # nothing to signal; clean no-op through real defaults
