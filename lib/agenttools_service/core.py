"""agenttools_service.core — one reusable service-manager for every long-running server.

WHAT THIS FILE IS
    The engine behind :class:`ServiceManager`. A :class:`Service` descriptor names a
    long-running server (its ``argv``, a default ``port``, and where its pidfile / logfile /
    autostart unit live). :class:`ServiceManager` gives that server the identical lifecycle
    subcommands every daemon in the ecosystem wants:

        run      — foreground/blocking (this shell), for ``disable``d / ad-hoc use.
        start    — background detached daemon; writes a pidfile, returns immediately.
        status   — {running, pid, port, url} from the pidfile + a liveness probe.
        stop     — stop the background instance, remove the pidfile.
        enable   — install OS autostart (launchd / systemd --user / a no-systemd fallback)
                   AND start it in the background now.
        disable  — remove autostart AND stop.

    A bare invocation (no subcommand) returns HELP — it NEVER launches anything.

HOW IT'S REACHED AT RUNTIME
    A consumer (``review dashboard``, ``config-web``, ``tg-ctl``, a future daemon) builds a
    :class:`Service`, wraps it in a :class:`ServiceManager`, and wires
    ``add_service_subcommands(...)`` into its argparse so ``<tool> <service>
    run|start|stop|status|enable|disable`` dispatches to the manager. The same machinery the
    roadmap wants shared by tg-ctl autostart + the daemon-supervisor (§3).

INVARIANTS / DESIGN
    - **Stdlib only at import time.** ``os`` / ``sys`` / ``shutil`` / ``subprocess`` /
      ``socket`` / ``pathlib`` / ``dataclasses`` / ``typing``. Zero third-party deps, so a
      consumer's ``--help`` stays fast and offline. The ONLY in-ecosystem dependency is
      ``agenttools_daemon`` (also stdlib-only), reused for the pidfile + start/stop/status
      machinery rather than reimplementing it (shared-util single source).
    - **Pidfile is the cross-process source of truth** — delegated to
      ``agenttools_daemon.Supervisor``. ``start`` writes it, ``stop`` reads+removes it,
      ``status`` reads+probes it. A ``stop`` in a different process finds the daemon via the
      file alone.
    - **Background = detached.** ``start`` spawns ``argv`` with ``start_new_session=True`` so
      the daemon outlives the launching shell; stdout/stderr go to the service's logfile.
    - **Autostart is pluggable + idempotent + removable.** ``enable``/``disable`` go through
      an :class:`AutostartBackend` chosen by OS: launchd LaunchAgent on macOS, a systemd
      ``--user`` unit on Linux with ``systemctl`` present, else a no-op fallback that still
      lets ``start``/``stop`` work (just no boot-survival). Re-running ``enable`` overwrites
      the unit in place (no duplicates); ``disable`` removes it and is a no-op if absent.
    - **Everything OS/process/path-related is injectable.** ``home`` / ``platform`` /
      ``has_systemctl`` / ``spawner`` / ``runner`` / ``alive`` are all parameters, so the
      whole manager — INCLUDING enable/disable and the launchd/systemd unit generation — is
      tested under a tmp HOME with fakes, touching no real autostart and no real process.

PAST BUGS THIS GUARDS AGAINST
    - A "service" CLI where the bare command launches the server is a footgun: a user typing
      ``review dashboard`` to read help accidentally starts a daemon. Here the bare path is
      HELP-only; launching requires an explicit ``start``/``run``/``enable``.
    - Hand-rolled autostart that appends a fresh LaunchAgent/unit on every ``enable`` leaves
      duplicates that fight at boot. ``enable`` is idempotent: it writes the unit to a
      deterministic path, overwriting.
    - Per-tool copies of this logic drift (slightly different pidfile formats, one forgets
      the systemd fallback, one launches on the bare command). One shared manager keeps every
      daemon's lifecycle identical.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence

# agenttools_daemon is the only in-ecosystem dep — also stdlib-only. Reused for the pidfile +
# start/stop/status machinery so this layer does not reimplement (and subtly diverge from) it.
from agenttools_daemon import AlreadyRunningError
from agenttools_daemon import Status as DaemonStatus
from agenttools_daemon import Supervisor

Argv = Sequence[str]

# ---------------------------------------------------------------------------------------
# OS detection — injectable, so tests pin a platform without a real OS.
# ---------------------------------------------------------------------------------------

MACOS = "darwin"
LINUX = "linux"


def current_platform() -> str:
    """The running platform key (``sys.platform``), normalized to our coarse buckets.

    Anything starting ``linux`` collapses to ``"linux"``; macOS is ``"darwin"``; everything
    else is returned verbatim (so the matrix can report it as unsupported-for-autostart).
    """
    plat = sys.platform
    if plat.startswith("linux"):
        return LINUX
    return plat


def _default_home() -> Path:
    """The user's home, honoring ``$HOME`` so a tmp-HOME test is fully isolated."""
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


# ---------------------------------------------------------------------------------------
# Service descriptor
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    """A long-running server's identity + where its runtime files live.

    Parameters
    ----------
    name
        The service id — used to name the pidfile, logfile, and autostart unit. Keep it a
        filesystem- and label-safe slug (``[A-Za-z0-9_.-]``); it is validated on construction.
    argv
        The command that runs the server **in the foreground** (the thing ``run`` blocks on
        and ``start`` detaches). A list/tuple of strings, e.g.
        ``["review", "dashboard", "run"]`` or ``[sys.executable, "-m", "mytool.server"]``.
    port
        The default TCP port the server listens on (used to build ``url`` for ``status``).
        ``None`` if the service is not a network server.
    host
        The host the ``url`` should point at (display only). Defaults to ``127.0.0.1``.
    tool
        The owning tool name, used to namespace the autostart label
        (``com.agenttools.<tool>.<name>`` / ``agenttools-<tool>-<name>``). Defaults to
        ``name`` when omitted.
    description
        A one-line human description, surfaced in the generated unit + ``--help``.
    home
        The home directory under which state/cache live. Injected by tests (a tmp HOME);
        defaults to ``$HOME``. The state dir (pid + autostart unit) is
        ``$XDG_STATE_HOME/<tool>`` (fallback ``~/.local/state/<tool>``); the cache dir
        (logs) is ``$XDG_CACHE_HOME/<tool>`` (fallback ``~/.cache/<tool>``).
    """

    name: str
    argv: Argv
    port: Optional[int] = None
    host: str = "127.0.0.1"
    tool: Optional[str] = None
    description: str = ""
    home: Optional[Path] = None

    def __post_init__(self) -> None:
        if not self.name or not _is_slug(self.name):
            raise ValueError(
                f"service name must be a non-empty slug [A-Za-z0-9_.-], got {self.name!r}"
            )
        # `tool` ends up in filesystem paths (state/cache dirs, plist/unit paths) and in the
        # autostart label, so it MUST be a slug too — an unvalidated `tool` like "../../etc"
        # would write pid/log/plist/unit files outside the intended dirs (arbitrary file
        # write for any consumer that sources `tool` from config/env). `_is_slug` rejects "/"
        # and any other separator; "." and ".." are rejected explicitly (they slip past a
        # naive slug check, and "." would also collapse the path).
        # `tool=""` (empty) is rejected too: `_is_slug("")` is vacuously True so it would slip
        # through, then `tool_name = self.tool or self.name` silently treats it as `None` — a
        # confusing accept-but-ignore. Pass `tool=None` to mean "default to name"; an empty
        # string is a bug, so fail closed.
        if self.tool is not None and (
            self.tool == "" or not _is_slug(self.tool) or self.tool in (".", "..")
        ):
            raise ValueError(
                f"tool must be a non-empty slug [A-Za-z0-9_.-] (not '.'/'..'), got {self.tool!r}"
            )
        if not self.name.strip("._-"):
            raise ValueError(f"service name must not be only separators, got {self.name!r}")
        # Coerce argv to a tuple FIRST: a generator passed as argv is truthy but is consumed by
        # the first iteration below, leaving a later `list(self.argv)` empty (a silent broken
        # daemon / empty ExecStart). A tuple is safe to re-iterate and keeps the frozen dataclass
        # hashable. `object.__setattr__` because the dataclass is frozen.
        # Reject a bare string BEFORE coercion: `tuple("review")` would silently become
        # ("r","e","v",…) — every per-char token is a valid 1-char str, so it sails through
        # and renders as `ExecStart=r e v i e w`. A str is a Sequence[str], so the type hint
        # won't catch it; reject explicitly.
        if isinstance(self.argv, (str, bytes)):
            raise ValueError("argv must be a list/tuple of strings, not a bare string")
        object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv or not all(isinstance(a, str) for a in self.argv):
            raise ValueError("argv must be a non-empty sequence of strings")
        if any(a == "" for a in self.argv):
            # an empty token would render as a bare `""` in ExecStart — almost always a bug
            # (a dropped variable), so fail closed rather than launch a malformed command.
            raise ValueError("argv tokens must be non-empty strings")
        # Reject control characters (notably newlines) in everything that is rendered into a
        # systemd unit / launchd plist or printed. A newline in `description` or an argv token
        # would otherwise inject arbitrary unit directives (`\n[Service]\nExecStartPre=…`) that
        # systemd runs at every login — arbitrary code execution for any consumer that sources
        # these from config. `name`/`tool` are already slug-constrained (no control chars get
        # past `_is_slug`), so this covers the free-text fields.
        for token in self.argv:
            if _has_control_char(token):
                raise ValueError(f"argv token must not contain control characters: {token!r}")
        if _has_control_char(self.description):
            raise ValueError("description must not contain control characters (e.g. newlines)")
        if self.port is not None and not (0 < int(self.port) < 65536):
            raise ValueError(f"port must be in 1..65535 or None, got {self.port!r}")
        # Reject a blank/control-char host AND any host carrying whitespace: a value like
        # "  127.0.0.1  " or "my host" would otherwise render into `url` as
        # "http://  127.0.0.1  :7878" — a silently broken URL. The host lands verbatim in the
        # URL, so it must be a single whitespace-free token; fail closed rather than emit a URL
        # no client can use.
        if (
            not self.host
            or _has_control_char(self.host)
            or any(c.isspace() for c in self.host)
        ):
            raise ValueError(
                "host must be a non-empty string with no whitespace or control characters"
            )

    # --- identity --------------------------------------------------------------------

    @property
    def tool_name(self) -> str:
        """The owning tool (``tool`` if set, else ``name``)."""
        return self.tool or self.name

    @property
    def label(self) -> str:
        """Reverse-DNS-ish autostart label, unique per (tool, service).

        macOS launchd wants a reverse-DNS label; systemd wants a unit name. We derive both
        from the same stem so a service is identifiable across either backend.
        """
        return f"com.agenttools.{self.tool_name}.{self.name}"

    @property
    def systemd_unit_name(self) -> str:
        """The systemd ``--user`` unit filename stem (``.service`` appended by the backend)."""
        return f"agenttools-{self.tool_name}-{self.name}"

    # --- paths -----------------------------------------------------------------------

    @property
    def _home(self) -> Path:
        return self.home or _default_home()

    @property
    def state_dir(self) -> Path:
        """Where the pidfile + autostart unit live: ``$XDG_STATE_HOME/<tool>``.

        Falls back to ``~/.local/state/<tool>``. Read from the environment on every access so
        a test's ``XDG_STATE_HOME`` (or a per-process override) is honored.
        """
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else self._home / ".local" / "state"
        return root / self.tool_name

    @property
    def cache_dir(self) -> Path:
        """Where the logfile lives: ``$XDG_CACHE_HOME/<tool>`` (fallback ``~/.cache/<tool>``)."""
        base = os.environ.get("XDG_CACHE_HOME")
        root = Path(base) if base else self._home / ".cache"
        return root / self.tool_name

    @property
    def pidfile(self) -> Path:
        return self.state_dir / f"{self.name}.pid"

    @property
    def logfile(self) -> Path:
        return self.cache_dir / f"{self.name}.log"

    @property
    def url(self) -> Optional[str]:
        """``http://<host>:<port>`` when a port is set, else ``None``."""
        if self.port is None:
            return None
        return f"http://{self.host}:{self.port}"


def _is_slug(text: str) -> bool:
    # ASCII-only: `str.isalnum()` accepts Unicode letters (é, ñ, CJK, …), which pass here but
    # produce invalid launchd labels / systemd unit names (both want ASCII). Enforce the
    # documented [A-Za-z0-9_.-] by requiring each char be ASCII as well as alnum.
    return all((c.isascii() and c.isalnum()) or c in "_.-" for c in text)


def _has_control_char(text: str) -> bool:
    """True if ``text`` contains any C0/C1 control char (newlines, tabs, NUL, etc.).

    Used to reject values that would break out of a single line in a rendered systemd unit /
    launchd plist (the unit-injection guard). ``\\t`` is included: a tab in ``ExecStart`` is a
    token separator and a stray one would split an argument.
    """
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text)


# ---------------------------------------------------------------------------------------
# Status — a thin view a CLI prints. Carries the daemon state plus port/url.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceStatus:
    """A snapshot a CLI prints for ``status``.

    ``running`` mirrors ``agenttools_daemon``'s liveness probe (pidfile present + pid alive).
    ``state`` is the underlying daemon state (``running`` / ``stale`` / ``stopped``).
    ``enabled`` reflects whether OS autostart is installed.
    """

    running: bool
    state: str
    pid: Optional[int]
    port: Optional[int]
    url: Optional[str]
    enabled: bool = False

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "url": self.url,
            "enabled": self.enabled,
        }


# ---------------------------------------------------------------------------------------
# Autostart backends
# ---------------------------------------------------------------------------------------


class AutostartBackend(Protocol):
    """A per-OS mechanism that makes a :class:`Service` start at login/boot.

    Idempotent and removable: ``install`` overwrites any prior unit (no duplicates),
    ``uninstall`` is a no-op when nothing is installed, ``is_installed`` reports presence.
    ``kind`` is a short tag for status/help (``"launchd"`` / ``"systemd"`` / ``"none"``).

    ``manages_process`` is the ownership flag that prevents a double-start: when ``True``
    (launchd / systemd), installing the unit ALSO launches the process now (launchd
    ``RunAtLoad`` / ``systemctl --user enable --now``), so the manager must NOT also spawn its
    own pidfile-tracked daemon — the OS is the single owner. When ``False`` (the no-autostart
    fallback) there is no OS to run anything, so the manager falls back to a pidfile ``start``.
    """

    kind: str
    manages_process: bool

    def install(self, service: Service) -> bool:  # pragma: no cover - structural
        """Write the autostart unit and register/launch it. ``True`` if fully successful.

        ``False`` means the unit file was written but the OS control command failed (e.g.
        ``launchctl``/``systemctl`` returned nonzero) — so ``is_installed`` may still be
        ``True`` on disk while the service is not actually under OS control.
        """
        ...

    def uninstall(self, service: Service) -> bool:  # pragma: no cover - structural
        """Remove the unit (and stop any OS-managed process). ``True`` if one was present."""
        ...

    def is_installed(self, service: Service) -> bool:  # pragma: no cover - structural
        ...

    def unit_path(self, service: Service) -> Path:  # pragma: no cover - structural
        ...


# --- unit generation (pure, directly tested) -------------------------------------------


def render_launchd_plist(service: Service) -> str:
    """Render a launchd LaunchAgent ``.plist`` for ``service`` (pure; no I/O).

    ``RunAtLoad`` starts it at load/login; ``KeepAlive`` restarts it **only on a non-clean
    exit** (``SuccessfulExit: false``) rather than unconditionally — a plain ``KeepAlive=true``
    would relaunch a service that exited 0 in a tight loop. This mirrors systemd's
    ``Restart=on-failure``. stdout/stderr are redirected to the logfile.

    PATH caveat: launchd runs ``ProgramArguments[0]`` with a minimal environment, NOT the
    user's shell ``PATH`` — a bare ``argv[0]`` like ``"review"`` may not resolve, and the
    agent silently won't start. This renderer is intentionally pure (no filesystem lookup), so
    it does NOT resolve ``argv[0]``; pass an ABSOLUTE ``argv[0]`` (e.g. ``shutil.which("review")``
    or ``sys.executable``) in the :class:`Service` for a reliable LaunchAgent.
    """
    args = "".join(f"\t\t<string>{_xml_escape(a)}</string>\n" for a in service.argv)
    log = _xml_escape(str(service.logfile))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>Label</key>\n"
        f"\t<string>{_xml_escape(service.label)}</string>\n"
        "\t<key>ProgramArguments</key>\n"
        f"\t<array>\n{args}\t</array>\n"
        "\t<key>RunAtLoad</key>\n"
        "\t<true/>\n"
        "\t<key>KeepAlive</key>\n"
        "\t<dict>\n"
        "\t\t<key>SuccessfulExit</key>\n"
        "\t\t<false/>\n"
        "\t</dict>\n"
        "\t<key>StandardOutPath</key>\n"
        f"\t<string>{log}</string>\n"
        "\t<key>StandardErrorPath</key>\n"
        f"\t<string>{log}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit(service: Service) -> str:
    """Render a systemd ``--user`` ``.service`` unit for ``service`` (pure; no I/O).

    ``Restart=on-failure`` survives crashes; ``WantedBy=default.target`` is the user-session
    equivalent of "enable at login". argv is rendered as a single ``ExecStart`` line with each
    token quoted so paths/args with spaces survive.

    Call ONLY with a validated :class:`Service`: this renderer escapes spacing and ``%``
    specifiers but does NOT and cannot neutralize newlines (systemd units are line-oriented —
    a newline terminates a directive even inside quotes). ``Service`` rejects control chars at
    construction; that validation is the actual injection guard.

    NOTE: ``%`` is a specifier introducer in EVERY directive value, not just ``ExecStart`` —
    so ``Description=`` and the ``append:`` log paths are ``%%``-escaped too. ``append:`` has no
    quoting syntax, so a SPACE in the log path (e.g. a username with a space) would truncate the
    path; that is a systemd limitation, documented rather than worked around.
    """
    exec_start = " ".join(_systemd_quote(a) for a in service.argv)
    desc = _systemd_escape_value(service.description
                                 or f"{service.tool_name} {service.name} service")
    log = _systemd_escape_value(str(service.logfile))
    return (
        "[Unit]\n"
        f"Description={desc}\n"
        # `network-online.target` (+ Wants) is the correct "wait for the network" ordering for
        # a --user service; bare `network.target` is a system-level target that gives a user
        # unit no real ordering guarantee, so a login-started service could race the network.
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _xml_escape(text: str) -> str:
    """Escape XML metacharacters, including quotes.

    All current interpolations land in element CONTENT, where ``"``/``'`` need no escaping —
    but escaping them anyway is free and means a future maintainer who moves a value into an
    attribute (``<string foo="...">``) can't introduce an injection. Order matters: ``&``
    first so we don't double-escape the entities we just produced.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _systemd_escape_value(value: str) -> str:
    """Escape a non-``ExecStart`` directive value (``Description=``, ``append:`` paths).

    ``%`` is a specifier introducer in every systemd directive value, so a literal ``%`` must
    be doubled or it is substituted with host/unit data at parse/display time. These directives
    take the rest of the line verbatim (no token splitting), so no quoting is needed — only the
    ``%`` doubling. (A space in an ``append:`` path still truncates it; systemd has no quoting
    there — that is a documented limitation, not something escaping can fix.)
    """
    return value.replace("%", "%%")


def _systemd_quote(token: str) -> str:
    """Quote a single ``ExecStart`` token for systemd, escaping its specifiers.

    systemd treats ``%`` as a specifier introducer inside ``ExecStart=`` (``%H`` → hostname,
    ``%u`` → user, …), so a literal ``%`` MUST be doubled to ``%%`` or it is silently
    substituted with host/user data — even inside quotes. We double ``%`` first, then quote
    the whole token if it contains whitespace/quotes/backslashes so paths and args with spaces
    survive intact.
    """
    token = token.replace("%", "%%")
    # Quote on whitespace/quote/backslash so tokens with spaces survive as one argument.
    # NOTE: quoting does NOT neutralize a newline — systemd unit files are line-oriented, so a
    # newline (even inside double quotes) terminates the ExecStart= assignment. The real guard
    # against newline injection is Service validation (`_has_control_char` rejects them at
    # construction); this function only handles spacing/escaping of well-formed tokens.
    if token and all(c not in token for c in ' \t"\\') and not _has_control_char(token):
        return token
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- concrete backends ------------------------------------------------------------------


@dataclass
class LaunchdBackend:
    """macOS autostart via a per-user LaunchAgent ``.plist`` + ``launchctl``.

    The plist lives at ``~/Library/LaunchAgents/<label>.plist``. ``launchctl`` calls go
    through an injected ``runner`` (defaults to a real ``subprocess.run``) so a test can
    assert the load/unload commands without touching the real ``launchctl``.

    NOTE: this uses the legacy ``launchctl load``/``unload`` verbs. They still work on current
    macOS but are deprecated in favor of ``launchctl bootstrap gui/$UID <path>`` /
    ``bootout gui/$UID/<label>``. Migration is deferred — the modern verbs need the user's UID
    and a domain target, which widens the seam; the load/unload shape is pinned by the test
    suite so the eventual switch is deliberate, not accidental.
    """

    home: Path
    runner: Callable[[Sequence[str]], int] = field(default=None)  # type: ignore[assignment]
    kind: str = "launchd"
    # launchd runs the job on load (RunAtLoad) and at every login — it OWNS the process when
    # enabled, so the manager must not also pidfile-spawn it (see ServiceManager.enable).
    manages_process: bool = True

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = _default_runner

    def unit_path(self, service: Service) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{service.label}.plist"

    def install(self, service: Service) -> bool:
        """Write the plist and ``launchctl load`` it (which launches it now via RunAtLoad).

        Returns ``True`` only if ``launchctl load`` succeeded — a nonzero rc (sandbox-blocked
        launchctl, malformed plist) yields ``False`` so the manager reports ``enabled=False``
        and warns, rather than pretending autostart is live just because the file is on disk.
        """
        path = self.unit_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        service.logfile.parent.mkdir(parents=True, exist_ok=True)
        # Overwrite-in-place = idempotent (no duplicate agents). Unload an old copy first so
        # launchctl doesn't refuse the reload, then load the fresh one.
        if path.exists():
            self.runner(["launchctl", "unload", str(path)])
        # Explicit UTF-8: write_text defaults to the locale encoding, so a non-UTF-8 locale
        # (C/POSIX, some CI) would raise UnicodeEncodeError on a legal non-ASCII description.
        path.write_text(render_launchd_plist(service), encoding="utf-8")
        rc = self.runner(["launchctl", "load", str(path)])
        return rc == 0

    def uninstall(self, service: Service) -> bool:
        path = self.unit_path(service)
        if not path.exists():
            return False
        # `launchctl unload` stops the running job AND removes it from launchd, so a separate
        # stop is unnecessary on macOS. Gate the file removal on its success: if unload failed
        # (sandbox-blocked, job loaded under a different path), the OS may still be running the
        # process — removing the file then would orphan it and make `is_installed` lie. Leave
        # the file in place and report failure so the caller knows the service is NOT stopped.
        rc = self.runner(["launchctl", "unload", str(path)])
        if rc != 0:
            return False
        path.unlink()
        return True

    def is_installed(self, service: Service) -> bool:
        return self.unit_path(service).exists()


@dataclass
class SystemdUserBackend:
    """Linux autostart via a systemd ``--user`` unit + ``systemctl --user``.

    The unit lives at ``~/.config/systemd/user/<unit>.service`` (honoring
    ``$XDG_CONFIG_HOME``). ``systemctl --user`` calls go through an injected ``runner``.
    """

    home: Path
    runner: Callable[[Sequence[str]], int] = field(default=None)  # type: ignore[assignment]
    kind: str = "systemd"
    # `enable --now` starts the unit now and at every login — systemd OWNS the process when
    # enabled, so the manager must not also pidfile-spawn it (see ServiceManager.enable).
    manages_process: bool = True

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = _default_runner

    def _unit_dir(self) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else self.home / ".config"
        return root / "systemd" / "user"

    def unit_path(self, service: Service) -> Path:
        return self._unit_dir() / f"{service.systemd_unit_name}.service"

    def install(self, service: Service) -> bool:
        """Write the unit and ``systemctl --user enable --now`` it (registers + starts now).

        ``--now`` is what makes ``enable`` actually run the process under systemd's tracking
        (so its declared ``Restart=on-failure`` supervision is live and ``systemctl stop``
        acts on the real MainPID). Returns ``True`` only if the ``enable --now`` succeeded.
        """
        path = self.unit_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        service.logfile.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(service), encoding="utf-8")  # overwrite=idempotent
        unit = f"{service.systemd_unit_name}.service"
        self.runner(["systemctl", "--user", "daemon-reload"])
        rc = self.runner(["systemctl", "--user", "enable", "--now", unit])
        return rc == 0

    def uninstall(self, service: Service) -> bool:
        path = self.unit_path(service)
        unit = f"{service.systemd_unit_name}.service"
        if not path.exists():
            return False
        # `disable --now` stops the running unit AND removes the login wiring, so a unit systemd
        # actually started (e.g. after a reboot) is brought down, not orphaned. Gate the file
        # removal on its success: if it failed, systemd may still be running the unit — leave
        # the file and report failure rather than orphaning the process and lying via
        # `is_installed`.
        rc = self.runner(["systemctl", "--user", "disable", "--now", unit])
        if rc != 0:
            return False
        path.unlink()
        self.runner(["systemctl", "--user", "daemon-reload"])
        return True

    def is_installed(self, service: Service) -> bool:
        return self.unit_path(service).exists()


@dataclass
class NoopAutostartBackend:
    """Fallback when the OS has no supported autostart (no systemd, or an unsupported OS).

    ``enable``/``disable`` still work for the foreground/background lifecycle — there is just
    no boot/login survival. ``install`` records nothing and returns a sentinel path; the
    manager reports ``enabled=False`` so a CLI can warn the user.
    """

    home: Path
    reason: str = "no supported OS autostart backend"
    kind: str = "none"
    # No OS to run anything → the manager owns the process via the pidfile path on enable.
    manages_process: bool = False

    def unit_path(self, service: Service) -> Path:
        return service.state_dir / f"{service.name}.autostart-unsupported"

    def install(self, service: Service) -> bool:
        return False  # nothing installed; the manager will pidfile-start instead

    def uninstall(self, service: Service) -> bool:
        return False

    def is_installed(self, service: Service) -> bool:
        return False


def select_autostart_backend(
    *,
    platform: str,
    home: Path,
    has_systemctl: Optional[bool] = None,
    runner: Optional[Callable[[Sequence[str]], int]] = None,
) -> AutostartBackend:
    """Pick the right autostart backend for ``platform``.

    - macOS (``darwin``)         → :class:`LaunchdBackend`.
    - Linux **with** systemctl   → :class:`SystemdUserBackend`.
    - Linux **without** systemctl, or any other OS → :class:`NoopAutostartBackend`.

    ``has_systemctl`` is probed with ``shutil.which("systemctl")`` when not given (injectable
    so a test can force either branch without a real systemd).
    """
    if platform == MACOS:
        return LaunchdBackend(home=home, runner=runner or _default_runner)
    if platform == LINUX:
        present = has_systemctl
        if present is None:
            present = shutil.which("systemctl") is not None
        if present:
            return SystemdUserBackend(home=home, runner=runner or _default_runner)
        return NoopAutostartBackend(home=home, reason="systemd (systemctl) not found")
    return NoopAutostartBackend(home=home, reason=f"unsupported platform {platform!r}")


def _default_runner(cmd: Sequence[str]) -> int:
    """Run an autostart control command, returning its exit code. Never raises on nonzero.

    A missing binary or a nonzero exit is reported as a nonzero return rather than an
    exception, so ``enable`` does not hard-fail on a quirky host — the unit file is still
    written and the backend reports the discrepancy via its ``install`` return value.
    stdout/stderr are DISCARDED (sent to ``DEVNULL``, not inherited) so a launchctl/systemctl
    warning cannot interleave with the manager's own one-line status output and break a script
    parsing ``dispatch`` output. Exit-code mapping follows the shell convention and keeps failure
    classes distinguishable: a **missing** binary → ``127``; any other ``OSError`` (e.g.
    permission denied, sandbox-blocked exec) → ``126`` — not collapsed together, so a caller
    can tell "not installed" from "installed but couldn't run it".
    """
    try:
        completed = subprocess.run(  # noqa: S603
            list(cmd),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode
    except FileNotFoundError:
        return 127
    except OSError:
        return 126


# ---------------------------------------------------------------------------------------
# ServiceManager
# ---------------------------------------------------------------------------------------


@dataclass
class ServiceManager:
    """Lifecycle operations for one :class:`Service`: run/start/status/stop/enable/disable.

    The pidfile + start/stop/status machinery is delegated to
    ``agenttools_daemon.Supervisor`` (one shared copy, not a reimplementation). Autostart is
    delegated to an :class:`AutostartBackend` chosen by OS. All OS/process seams are
    injectable so the whole manager is testable under a tmp HOME with no real side effects.

    Parameters
    ----------
    service
        The :class:`Service` to manage.
    platform
        The platform key (defaults to :func:`current_platform`); pinned by tests.
    has_systemctl
        Whether ``systemctl`` is available (Linux autostart selection). ``None`` ⇒ probe.
    autostart
        An explicit :class:`AutostartBackend` (overrides OS selection); mainly for tests.
        NOTE: when you pass ``autostart=`` explicitly, ``runner=`` is NOT applied to it (the
        explicit backend carries its own ``runner``) — set the runner on the backend you pass.
        ``runner=`` only configures the auto-selected backend.
    spawner
        ``Callable[[argv, logfile], Child]`` that detaches the background daemon. Defaults to
        a ``subprocess.Popen`` with ``start_new_session=True`` and stdout/stderr → logfile.
        ``start_new_session`` is POSIX-only; on Windows the spawned process is not fully
        detached (pass a Windows-specific spawner if you need boot/login survival there).
    runner
        The autostart control-command runner passed to the AUTO-SELECTED backend. Ignored when
        ``autostart=`` is given explicitly (that backend owns its own runner).
    alive
        Liveness probe passed through to the Supervisor (``os.kill(pid, 0)`` by default).
    signaller
        ``Callable[[pid, sig], None]`` passed through to the Supervisor for the cross-process
        ``stop`` signal (``os.kill`` by default). Injected in tests.
    foreground_runner
        ``Callable[[argv], int]`` used by ``run`` to block on the foreground process.
        Defaults to ``subprocess.call`` (returns the exit code). Injected in tests.
    """

    service: Service
    platform: str = field(default_factory=current_platform)
    has_systemctl: Optional[bool] = None
    autostart: Optional[AutostartBackend] = None
    spawner: Optional[Callable[[Argv, Path], object]] = None
    runner: Optional[Callable[[Sequence[str]], int]] = None
    alive: Optional[Callable[[int], bool]] = None
    signaller: Optional[Callable[[int, int], None]] = None
    foreground_runner: Optional[Callable[[Argv], int]] = None

    _backend: AutostartBackend = field(init=False, repr=False)
    # Same-process record of whether the most recent enable's OS control command succeeded.
    # True by default so a plain `status` (no enable yet) treats an installed unit as active.
    _last_enable_ok: bool = field(default=True, init=False, repr=False)
    # Same-process record of whether the most recent disable's uninstall fully succeeded.
    _last_disable_ok: bool = field(default=True, init=False, repr=False)

    def __post_init__(self) -> None:
        self._backend = self.autostart or select_autostart_backend(
            platform=self.platform,
            home=self.service._home,
            has_systemctl=self.has_systemctl,
            runner=self.runner,
        )
        if self.spawner is None:
            self.spawner = _default_detached_spawner
        if self.foreground_runner is None:
            self.foreground_runner = _default_foreground_runner

    # --- helpers ---------------------------------------------------------------------

    @property
    def backend(self) -> AutostartBackend:
        """The selected autostart backend (for status/help)."""
        return self._backend

    def _supervisor(self) -> Supervisor:
        """A Supervisor bound to this service's argv + pidfile, with our detached spawner.

        A FRESH Supervisor is built on every call (``start``/``status``/``stop``/``enable``/
        ``disable``). This is correct precisely because the pidfile is the sole source of truth:
        the Supervisor's only durable state is the pidfile, so a new instance reads the same
        truth a previous one wrote. The manager intentionally keeps no in-memory ``Child``
        handle — a background daemon outlives the launching process, so an in-memory handle
        would be useless to a later ``stop`` in a different process anyway.
        """
        sup = Supervisor(
            list(self.service.argv),
            pidfile=str(self.service.pidfile),
            spawner=self._spawn_for_supervisor,
        )
        # Override the Supervisor's process seams only when the manager was given explicit ones
        # (tests inject fakes; production leaves them None → the Supervisor's real os.kill-based
        # defaults stand). Set post-construction rather than via a `**kwargs` dict so each
        # assignment keeps its own precise callable type (alive: (int)->bool vs signaller:
        # (int,int)->None) instead of being unified into one loose dict value type.
        if self.alive is not None:
            sup.alive = self.alive
        if self.signaller is not None:
            sup.signaller = self.signaller
        return sup

    def _spawn_for_supervisor(self, cmd: Argv):
        """Adapt our (argv, logfile) detached spawner to the Supervisor's (cmd)->Child seam."""
        self.service.logfile.parent.mkdir(parents=True, exist_ok=True)
        return self.spawner(list(cmd), self.service.logfile)  # type: ignore[misc]

    # --- public API ------------------------------------------------------------------

    def run(self) -> int:
        """Run the service in the FOREGROUND (this shell), blocking. Returns its exit code.

        For ``disable``d / ad-hoc use. Does not write a pidfile (this shell IS the service).
        """
        return int(self.foreground_runner(list(self.service.argv)))  # type: ignore[misc]

    def start(self) -> ServiceStatus:
        """Start the service as a BACKGROUND detached daemon; return immediately.

        Writes the pidfile (via the Supervisor) and returns a :class:`ServiceStatus`. Raises
        ``agenttools_daemon.AlreadyRunningError`` if a live instance is already recorded.
        """
        sup = self._supervisor()
        sup.start()
        return self.status()

    def status(self) -> ServiceStatus:
        """Report {running, pid, port, url, enabled} from the pidfile + a liveness probe.

        ``running``/``pid``/``state`` come from the pidfile + a liveness probe — i.e. they
        track the MANAGER-spawned (``start``) instance. When OS autostart owns the process
        (``enable`` on launchd/systemd), that instance has no pidfile, so ``running`` reflects
        the autostart-INSTALLED state instead. ``enabled`` is ``True`` only when the OS unit is
        installed.

        LIMITATION: for an OS-managed (enabled) service, ``running`` is inferred from "the unit
        is installed", NOT from probing the actual launchd/systemd process. If that process
        crashed in a way the OS won't auto-restart (a clean ``exit 0``, or a SIGKILL — neither
        triggers ``KeepAlive{SuccessfulExit:false}`` / ``Restart=on-failure``), ``status`` can
        report ``running=True`` while nothing is up. Probing the live OS process would need a
        ``launchctl list`` / ``systemctl is-active`` parse (a separate seam) — deliberately out
        of scope here; use the OS's own tools for authoritative OS-managed liveness.
        """
        sup = self._supervisor()
        st: DaemonStatus = sup.status()
        enabled = self._backend.is_installed(self.service)
        # An OS-managed (enabled) service is "running" even though no pidfile names it — the OS
        # is the supervisor. Surface that so `status` after `enable` doesn't read "stopped".
        running = st.running or (enabled and self._backend.manages_process)
        return ServiceStatus(
            running=running,
            state="autostart" if (enabled and self._backend.manages_process and not st.running)
            else st.state,
            pid=st.pid,
            port=self.service.port,
            url=self.service.url,
            enabled=enabled,
        )

    def stop(self) -> bool:
        """Stop the background instance and remove the pidfile. Idempotent.

        Returns ``True`` if a live process was signalled, ``False`` if nothing was running.
        Stops only the MANAGER-spawned instance; an OS-autostart-owned process is stopped by
        ``disable`` (via the backend's ``uninstall``), not here.
        """
        return self._supervisor().stop()

    def autostart_active(self) -> bool:
        """Whether OS autostart is BOTH installed AND was last set up successfully.

        ``status().enabled`` is the durable, file-on-disk truth (the unit IS installed) — it
        stays ``True`` after a ``launchctl load`` / ``systemctl enable`` that returned nonzero,
        because the file is there and may still load at next login. This method is the
        same-process, "did the control command actually succeed" signal: it ANDs the
        file-presence with the result of the most recent ``enable`` in this process
        (``True`` when no ``enable`` has run, so a plain ``status`` after a clean enable reads
        active). ``run_action`` uses it to avoid printing "enabled" when the OS command failed.
        """
        return self._backend.is_installed(self.service) and self._last_enable_ok

    @property
    def last_disable_ok(self) -> bool:
        """Whether the most recent ``disable`` fully removed autostart (same-process signal).

        ``False`` means the backend's ``uninstall`` reported failure (the OS unload/disable
        command returned nonzero) — the unit may still be loaded and the process still running.
        ``True`` when nothing was installed (no-op) or the removal succeeded.
        """
        return self._last_disable_ok

    def enable(self) -> ServiceStatus:
        """Install OS autostart AND start the service now. Idempotent.

        Single-owner, no double-start: on a backend that manages the process
        (launchd/systemd), installing the unit ALSO launches it now (``RunAtLoad`` /
        ``enable --now``), so the manager does NOT also pidfile-spawn it — that would put two
        processes on the same port. On the no-autostart fallback OS there is no OS to run
        anything, so the manager falls back to a background ``start`` (writing the pidfile);
        ``status().enabled`` stays ``False`` and the caller should warn it won't survive reboot.

        Order is install-then-fallback-start so a manager-owned start failure can't leave a
        half-enabled state with no running process for the backends that manage it.
        """
        if self._backend.manages_process:
            # The OS is about to launch its own copy (RunAtLoad / enable --now). Stop any
            # manager-spawned (pidfile) instance first so we don't end up with two processes
            # on the same port — the exact no-double-start invariant. No-op if none is running.
            self._supervisor().stop()
        installed_ok = self._backend.install(self.service)
        # Remember the OS control command's outcome so `autostart_active` / `run_action` can
        # distinguish "unit on disk" from "OS actually accepted it" (a failed launchctl/
        # systemctl leaves the file but does not make autostart live).
        self._last_enable_ok = installed_ok or not self._backend.manages_process
        if not self._backend.manages_process:
            # No OS supervisor — the manager owns the process. Start if not already up.
            if not self._supervisor().status().running:
                self._supervisor().start()
        elif not installed_ok:
            # The OS backend exists but the control command failed (sandbox/missing binary);
            # the unit file may be on disk but nothing is actually running under the OS. Fall
            # back to a manager-spawned instance so `enable` still leaves the service UP.
            if not self._supervisor().status().running:
                self._supervisor().start()
        return self.status()

    def disable(self) -> ServiceStatus:
        """Remove OS autostart AND stop the running instance. Idempotent.

        Uninstalling the OS unit stops the OS-managed process (``launchctl unload`` /
        ``systemctl --user disable --now``); ``stop()`` then also brings down any
        manager-spawned (pidfile) instance — covering a service that was ``start``ed manually
        and then ``enable``d, or one started via the fallback path. Both halves are no-ops when
        absent, so ``disable`` is safe on an already-disabled / already-stopped service.

        If the backend's ``uninstall`` reports failure (the OS unload/disable command returned
        nonzero — the unit may still be loaded and running), that is recorded so ``run_action``
        reports the failure instead of a false "disabled and stopped".
        """
        was_installed = self._backend.is_installed(self.service)
        uninstalled = self._backend.uninstall(self.service)
        # disable "succeeded" if nothing was installed (nothing to do) or uninstall fully
        # removed it; a present-but-failed uninstall is a failure.
        self._last_disable_ok = (not was_installed) or uninstalled
        self.stop()
        return self.status()


# --- default detached / foreground spawners --------------------------------------------


def _default_detached_spawner(argv: Argv, logfile: Path):
    """Spawn ``argv`` as a detached background daemon, stdout/stderr → ``logfile``.

    ``start_new_session=True`` puts the child in its own session so it outlives the launching
    shell (the shell's SIGHUP on exit doesn't reach it). The log is opened append so restarts
    accumulate rather than truncate. Returns the ``Popen`` (a valid ``agenttools_daemon.Child``).
    """
    logfile.parent.mkdir(parents=True, exist_ok=True)
    # Open the log, hand it to the child, then close the PARENT's copy: Popen dup()s the fd
    # into the child, so the parent's handle is redundant and would otherwise leak one fd per
    # start. The child keeps its own dup'd fd open for as long as it runs.
    with open(logfile, "ab") as log:
        return subprocess.Popen(  # noqa: S603
            list(argv),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def _default_foreground_runner(argv: Argv) -> int:
    """Run ``argv`` in the foreground and block until it exits; return its exit code."""
    return subprocess.call(list(argv))  # noqa: S603


# ---------------------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------------------

# The lifecycle subcommands, in the order a CLI should present them.
SUBCOMMANDS = ("run", "start", "stop", "status", "enable", "disable")


def add_service_subcommands(
    subparsers,
    *,
    manager_factory: Callable[[], ServiceManager],
    service_name: str,
    help_text: Optional[Mapping[str, str]] = None,
) -> None:
    """Wire ``run|start|stop|status|enable|disable`` for one service into an argparse tree.

    ``subparsers`` is the result of ``parser.add_subparsers(...)`` for the SERVICE level
    (e.g. ``review dashboard <here>``). Each lifecycle subcommand gets a parser whose
    ``func`` is set to a thin handler that builds the manager (via ``manager_factory``) and
    calls the matching method, printing a one-line result. ``manager_factory`` is a no-arg
    callable so the manager is constructed lazily (only when a subcommand actually runs),
    keeping ``--help`` cheap.

    Wire the SERVICE-level parser so a bare invocation (no subcommand) prints HELP, never
    launches — see :func:`dispatch`.

    ``help_text`` optionally OVERRIDES the per-subcommand help with a mapping of
    ``{subcommand: help string}`` (e.g. ``{"start": "boot the dashboard"}``). Keys not present
    fall back to the built-in :data:`_SUBCOMMAND_HELP`, so you can override just one line.
    """
    for name in SUBCOMMANDS:
        override = help_text.get(name) if help_text else None
        sub = subparsers.add_parser(
            name,
            help=override or _SUBCOMMAND_HELP[name].format(service=service_name),
        )
        sub.set_defaults(_service_action=name, _service_manager_factory=manager_factory)


def dispatch(args, *, on_no_subcommand: Callable[[], int]) -> int:
    """Run the lifecycle action chosen on ``args`` (set by :func:`add_service_subcommands`).

    If no subcommand was chosen (bare ``<tool> <service>``), call ``on_no_subcommand`` (which
    should print HELP and return an exit code) — NEVER launch. Otherwise build the manager and
    invoke the matching method, printing a human-readable result, and return an exit code.
    """
    action = getattr(args, "_service_action", None)
    if action is None:
        return int(on_no_subcommand())
    factory = getattr(args, "_service_manager_factory")
    manager = factory()
    return run_action(manager, action)


def run_action(manager: ServiceManager, action: str) -> int:
    """Invoke ``action`` on ``manager``, print a one-line human result, return an exit code.

    Exit codes: ``0`` on success; ``3`` when ``status``/``stop`` find nothing running (so a
    script can branch on "was it up?"); ``4`` when ``enable``/``disable`` could not set up /
    tear down OS autostart (the launchctl/systemctl command failed). NOTE: ``run`` returns the
    RAW exit code of the foreground child, which may itself be ``3``/``4`` — the 3/4 contract
    above applies only to the manager-driven actions, not to ``run``'s passthrough. Success
    lines go to stdout; failure lines go to stderr (Unix convention). Kept tiny + side-effecting
    so a consumer can call it directly or let :func:`dispatch` call it.
    """
    svc = manager.service
    if action == "run":
        try:
            return manager.run()
        except FileNotFoundError:
            # The foreground binary (argv[0]) doesn't exist. `start` already gives a clean
            # message for its common failure (already-running); do the same here instead of
            # letting a raw traceback escape through dispatch. 127 = "command not found"
            # (the shell convention), so a script can branch on it.
            _eprint(f"{svc.name}: command not found: {svc.argv[0]!r}")
            return 127
    if action == "start":
        try:
            st = manager.start()
        except AlreadyRunningError as exc:
            # Clean CLI message instead of a raw traceback for the common "already up" case.
            _eprint(f"{svc.name}: already running (pid {exc.pid})")
            return 3
        _print(f"{svc.name}: started{_pid_suffix(st)}{_url_suffix(st)}")
        _hint_enable(manager)
        return 0
    if action == "status":
        st = manager.status()
        if st.running:
            _print(f"{svc.name}: running{_pid_suffix(st)}{_url_suffix(st)}"
                    f"{' [autostart on]' if st.enabled else ''}")
            return 0
        _print(f"{svc.name}: {st.state}")
        return 3
    if action == "stop":
        stopped = manager.stop()
        if stopped:
            _print(f"{svc.name}: stopped")
            return 0
        # No pidfile-tracked instance. But if OS autostart owns the process (enabled on a
        # launchd/systemd backend), a bare "was not running" is misleading — the service is
        # very likely alive under the OS, and `stop` does not touch the OS-managed process
        # (that's `disable`'s job). Warn on stderr so a script reading stdout/exit-3 isn't
        # fooled into thinking nothing is up.
        if manager.backend.manages_process and manager.status().enabled:
            _eprint(
                f"{svc.name}: no background instance to stop, but OS autostart "
                f"({manager.backend.kind}) is enabled — run 'disable' to stop the "
                f"OS-managed process"
            )
        else:
            _print(f"{svc.name}: was not running")
        return 3
    if action == "enable":
        st = manager.enable()
        where = manager.backend.kind
        # `autostart_active` is true only when the unit is installed AND the OS control command
        # accepted it — distinct from `st.enabled` (file on disk), which stays true even after a
        # failed launchctl/systemctl. Don't claim "enabled" when the OS rejected it.
        if manager.autostart_active():
            _print(f"{svc.name}: autostart enabled ({where}), "
                   f"running{_pid_suffix(st)}{_url_suffix(st)}")
            return 0
        if st.enabled:
            # file written but the OS command failed; we fell back to a manager-spawned start.
            _eprint(
                f"{svc.name}: started{_pid_suffix(st)}, but enabling autostart ({where}) "
                f"FAILED — it may not survive reboot; check the {where} unit"
            )
            return 4
        # not a failure — the OS just has no autostart; warn on stderr so stdout stays clean.
        _eprint(
            f"{svc.name}: started{_pid_suffix(st)}, but autostart is unavailable "
            f"on this OS ({where}) — it will NOT survive reboot"
        )
        return 0
    if action == "disable":
        manager.disable()
        if manager.last_disable_ok:
            _print(f"{svc.name}: autostart disabled and stopped")
            return 0
        _eprint(
            f"{svc.name}: FAILED to remove autostart ({manager.backend.kind}) — "
            f"the unit may still be loaded; check it manually"
        )
        return 4
    raise ValueError(f"unknown service action: {action!r}")


def _url_suffix(st: ServiceStatus) -> str:
    return f" — {st.url}" if st.url else ""


def _pid_suffix(st: ServiceStatus) -> str:
    """`` (pid N)`` when a pidfile-tracked pid exists; empty when the OS owns the process."""
    return f" (pid {st.pid})" if st.pid is not None else ""


def _hint_enable(manager: ServiceManager) -> None:
    """Print the 'run … enable to start at login' hint after a foreground/background start.

    The command is built from the service's own ``tool``/``name`` (e.g. ``review dashboard
    enable``) — a best-effort guess at the consumer's CLI shape, which is right whenever the
    tool's argv mirrors ``<tool> <service>`` (the convention this lib targets).
    """
    svc = manager.service
    if not manager.backend.is_installed(svc) and manager.backend.kind != "none":
        cmd = f"{svc.tool_name} {svc.name}" if svc.tool else svc.name
        _print(f"  hint: run '{cmd} enable' to start it at login")


def _print(msg: str) -> None:
    print(msg)


def _eprint(msg: str) -> None:
    """Print an error/warning line to stderr (Unix convention), keeping stdout for results."""
    print(msg, file=sys.stderr)


_SUBCOMMAND_HELP = {
    "run": "run {service} in the foreground (this shell), blocking",
    "start": "start {service} in the background (detached daemon)",
    "stop": "stop the background {service} instance",
    "status": "show whether {service} is running (pid/port/url)",
    "enable": "install OS autostart for {service} AND start it now",
    "disable": "remove OS autostart for {service} AND stop it",
}


__all__ = [
    "Service",
    "ServiceManager",
    "ServiceStatus",
    "AutostartBackend",
    "LaunchdBackend",
    "SystemdUserBackend",
    "NoopAutostartBackend",
    "select_autostart_backend",
    "render_launchd_plist",
    "render_systemd_unit",
    "current_platform",
    "add_service_subcommands",
    "dispatch",
    "run_action",
    "SUBCOMMANDS",
    "MACOS",
    "LINUX",
]
