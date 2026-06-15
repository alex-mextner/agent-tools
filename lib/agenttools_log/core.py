"""Core implementation of the structured JSONL logger.

The public surface (``get_logger``, ``configure``, ``StructuredLogger``, ``reset``) is
re-exported from the package ``__init__``; import from there, not from this module.

Design notes
------------
* One shared, namespaced root logger (``agenttools``). Per-module loggers are children of
  it (``agenttools.<name>``) so a single handler/formatter/level governs the whole tree —
  configured exactly once, idempotently.
* The formatter renders the stdlib ``LogRecord`` as a one-line JSON object. Structured
  fields arrive via ``extra={_FIELDS_KEY: {...}}``; :class:`StructuredLogger` wraps that so
  callers pass plain kwargs.
* Safe by construction: the formatter never raises (a bad field is stringified, not
  propagated), and a file sink is forced to ``0600`` so it mirrors review-cli's
  ``stats.py`` privacy posture even for a pre-existing file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any

# Re-exported level constants so consumers don't have to import stdlib ``logging`` too.
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR

# Root of the logger tree. Every get_logger(name) returns a child "agenttools.<name>".
_ROOT_NAME = "agenttools"

# The key under which structured fields travel on a LogRecord (via ``extra=``).
_FIELDS_KEY = "agenttools_fields"

# Fields that are part of the canonical record and must never be overwritten by a caller's
# structured field (a stray ``msg=`` kwarg must not clobber the message, etc.).
_RESERVED = frozenset({"ts", "level", "logger", "msg"})

# stdlib LogRecord attributes — used to tell caller-supplied fields apart from the noise
# the logging machinery always sets, so ``%(...)`` and ``extra=`` both keep working.
_LOGRECORD_BUILTINS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", _FIELDS_KEY}

_LEVEL_NAMES = {
    "debug": DEBUG,
    "info": INFO,
    "warn": WARNING,
    "warning": WARNING,
    "error": ERROR,
}


class _Unset:
    """Sentinel distinct from ``None`` so ``configure(log_file=None)`` can EXPLICITLY mean
    'no file, ignore $AGENTTOOLS_LOG_FILE' — separate from 'argument omitted, use env'."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<unset>"


_UNSET = _Unset()


class _OwningStreamHandler(logging.StreamHandler):
    """A ``StreamHandler`` that owns its stream and closes it on ``close()``.

    The stdlib ``StreamHandler`` deliberately does NOT close its stream (it may be stderr).
    Our file sink hands it a private file object whose fd we DO want released on
    ``reset()``/reconfigure — otherwise the fd leaks until GC. This subclass closes it.
    """

    def close(self) -> None:
        try:
            stream = self.stream
            super().close()
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def _coerce_level(value: int | str | None, default: int = INFO) -> int:
    """Map a level name/number to a stdlib level int. Unknown -> ``default``."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return _LEVEL_NAMES.get(str(value).strip().lower(), default)


class JsonlFormatter(logging.Formatter):
    """Render a ``LogRecord`` as one line of JSON. Never raises."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            # Structured fields passed via the StructuredLogger wrapper.
            fields = getattr(record, _FIELDS_KEY, None)
            if isinstance(fields, dict):
                for key, val in fields.items():
                    if key not in _RESERVED:
                        payload[key] = val
            # Fields passed as bare ``extra={...}`` (stdlib-native usage) are surfaced too.
            for key, val in record.__dict__.items():
                if key not in _LOGRECORD_BUILTINS and key not in payload:
                    payload[key] = val
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            if record.stack_info:
                payload["stack"] = self.formatStack(record.stack_info)
            return json.dumps(payload, ensure_ascii=False, default=_safe_json_default)
        except Exception:  # noqa: BLE001 — logging must never crash the caller
            # Last-ditch: emit a minimal, always-valid line rather than propagating.
            return json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": getattr(record, "levelname", "ERROR"),
                    "logger": getattr(record, "name", _ROOT_NAME),
                    "msg": "<agenttools_log: failed to format record>",
                }
            )


class PrettyFormatter(logging.Formatter):
    """Human-readable single line for local dev. Appends structured fields as ``k=v``."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%H:%M:%S"
            )
            # Escape control chars in the message so a newline/CR can't forge a log line.
            msg = _one_line(record.getMessage())
            base = f"{ts} {record.levelname:<5} {record.name}: {msg}"
            extras: dict[str, Any] = {}
            fields = getattr(record, _FIELDS_KEY, None)
            if isinstance(fields, dict):
                extras.update(fields)
            for key, val in record.__dict__.items():
                if key not in _LOGRECORD_BUILTINS and key not in extras:
                    extras[key] = val
            tail = " ".join(
                f"{k}={_compact(v)}" for k, v in extras.items() if k not in _RESERVED
            )
            line = f"{base}  {tail}" if tail else base
            if record.exc_info:
                line += "\n" + self.formatException(record.exc_info)
            if record.stack_info:
                line += "\n" + self.formatStack(record.stack_info)
            return line
        except Exception:  # noqa: BLE001 — logging must never crash the caller
            return f"{getattr(record, 'levelname', 'ERROR')} <pretty-format-failed>"


def _safe_json_default(obj: Any) -> str:
    """Stringify anything ``json`` can't serialize, so a bad field never raises."""
    try:
        return str(obj)
    except Exception:  # noqa: BLE001
        return "<unserializable>"


def _one_line(text: str) -> str:
    """Render text on a single physical line for pretty mode.

    Control characters (newline, carriage return, tab, other C0 controls) are escaped to
    their backslash form so a value can't forge extra lines and break line-oriented log
    parsing. Printable text is returned unchanged.
    """
    if not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _compact(value: Any) -> str:
    """Compact repr of a field value for pretty mode. Always single-line, never raises."""
    try:
        if isinstance(value, str):
            # json.dumps both quotes-when-needed AND escapes control chars to \n/\t/etc.
            return _one_line(value) if " " not in value else json.dumps(value)
        return _one_line(json.dumps(value, default=_safe_json_default))
    except Exception:  # noqa: BLE001
        return "<unserializable>"


class StructuredLogger:
    """Thin per-module facade over a stdlib ``logging.Logger``.

    Wraps the canonical levels so callers pass structured fields as plain kwargs::

        log = get_logger(__name__)
        log.info("user login", user_id=42, ok=True)

    Reserved kwargs are passed through to the stdlib call rather than logged as fields:
    ``exc_info``, ``stack_info``, ``stacklevel``. Everything else becomes a JSON field.
    Every method is best-effort and never raises out of the logging path.
    """

    __slots__ = ("_logger",)

    # stdlib log()-call kwargs that must NOT be treated as structured fields.
    _PASSTHROUGH = frozenset({"exc_info", "stack_info", "stacklevel"})

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        return self._logger.name

    @property
    def stdlib(self) -> logging.Logger:
        """The underlying stdlib logger, for callers that need native access."""
        return self._logger

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 — mirror stdlib name
        try:
            return self._logger.isEnabledFor(level)
        except Exception:  # noqa: BLE001
            return False

    def _emit(self, level: int, msg: object, args: tuple, /, **kwargs: Any) -> None:
        try:
            if not self._logger.isEnabledFor(level):
                return
            passthrough = {
                k: kwargs.pop(k) for k in list(kwargs) if k in self._PASSTHROUGH
            }
            # ``*args`` feed stdlib's lazy ``%``-formatting (``log.info("x=%s", x)``); the
            # formatters call ``record.getMessage()`` which applies them. ``kwargs`` are
            # structured fields, kept separate under our extra key.
            self._logger.log(
                level,
                msg,
                *args,
                extra={_FIELDS_KEY: kwargs},
                **passthrough,
            )
        except Exception:  # noqa: BLE001 — a logging failure must never crash the caller
            pass

    # ``msg`` is positional-only (``/``) so a structured field named ``msg`` (or any
    # reserved name) can be passed as a kwarg without colliding with the parameter. ``*args``
    # support stdlib-style lazy ``%`` formatting; structured fields are passed as kwargs.
    def debug(self, msg: object, /, *args: Any, **fields: Any) -> None:
        self._emit(DEBUG, msg, args, **fields)

    def info(self, msg: object, /, *args: Any, **fields: Any) -> None:
        self._emit(INFO, msg, args, **fields)

    def warn(self, msg: object, /, *args: Any, **fields: Any) -> None:
        self._emit(WARNING, msg, args, **fields)

    def warning(self, msg: object, /, *args: Any, **fields: Any) -> None:
        """Alias for :meth:`warn` (stdlib's preferred spelling)."""
        self._emit(WARNING, msg, args, **fields)

    def error(self, msg: object, /, *args: Any, **fields: Any) -> None:
        self._emit(ERROR, msg, args, **fields)

    def exception(self, msg: object, /, *args: Any, **fields: Any) -> None:
        """Log at ERROR with the current exception traceback attached."""
        fields.setdefault("exc_info", True)
        self._emit(ERROR, msg, args, **fields)


# --- shared configuration state ---------------------------------------------------------

_CONFIGURED = False
# Serializes the check/build/add/set-flag sequence so two threads racing the first
# ``get_logger()`` can't both install a handler (duplicate lines + leaked fds). Reentrant
# so configure() can be called from within a lock-held path without deadlocking.
_CONFIG_LOCK = threading.RLock()


def _root() -> logging.Logger:
    return logging.getLogger(_ROOT_NAME)


def configure(
    *,
    level: int | str | None = None,
    fmt: str | None = None,
    stream: Any = _UNSET,
    log_file: str | os.PathLike[str] | None = _UNSET,
    force: bool = True,
) -> logging.Logger:
    """Configure the shared ``agenttools`` logger tree. Idempotent, never raises.

    Omitted arguments default to the environment (``AGENTTOOLS_LOG_LEVEL`` /
    ``AGENTTOOLS_LOG_FORMAT`` / ``AGENTTOOLS_LOG_FILE``); explicitly-passed arguments win
    over it so a CLI flag can override. Returns the stdlib root logger of the tree.

    * ``level`` — ``debug``/``info``/``warn``/``error`` or a stdlib int. Default ``info``.
    * ``fmt``   — ``json`` (default) or ``pretty`` for human-readable dev output.
    * ``stream``   — a writable stream for the handler (default ``sys.stderr``). Passing an
      explicit ``stream`` suppresses ``$AGENTTOOLS_LOG_FILE``; ignored only when an explicit
      ``log_file`` is also given (a file sink wins over a stream).
    * ``log_file`` — path to append JSONL to; created/forced ``0600``. Pass ``None``
      EXPLICITLY to force a stream sink even when ``$AGENTTOOLS_LOG_FILE`` is set.
    * ``force`` — when ``True`` (default) replace any previously installed handlers
      (re-configure cleanly); when ``False``, no-op if the tree is already configured, so a
      racing first ``get_logger()`` can never duplicate handlers.

    A failure to open the requested file sink falls back to stderr rather than crashing,
    and a failed ``chmod`` closes the handle and falls back to stderr too — never leave a
    world-readable log file behind.
    """
    # Hold the lock across the whole check->build->add->set-flag sequence so concurrent
    # first-use auto-config (or two configure() calls) can never both install a handler.
    with _CONFIG_LOCK:
        return _configure_locked(
            level=level, fmt=fmt, stream=stream, log_file=log_file, force=force
        )


def _configure_locked(
    *,
    level: int | str | None,
    fmt: str | None,
    stream: Any,
    log_file: str | os.PathLike[str] | None,
    force: bool,
) -> logging.Logger:
    """The body of :func:`configure`, run under ``_CONFIG_LOCK``. Never raises."""
    global _CONFIGURED
    try:
        logger = _root()

        # force=False means "configure once": if WE already configured the tree, leave it
        # be so a second call (or a racing auto-config) cannot duplicate every log line.
        # Key on our own _CONFIGURED flag, NOT logger.handlers — foreign code (e.g. pytest's
        # logging plugin) attaches its own handlers to every logger and must not be mistaken
        # for our configuration.
        if not force and _CONFIGURED:
            return logger

        env_level = os.environ.get("AGENTTOOLS_LOG_LEVEL")
        env_fmt = os.environ.get("AGENTTOOLS_LOG_FORMAT")
        env_file = os.environ.get("AGENTTOOLS_LOG_FILE")

        # Sentinels let us tell "argument omitted -> use env" apart from "explicitly None ->
        # no file". An explicit stream also suppresses the env file (explicit beats env).
        stream_given = stream is not _UNSET
        if log_file is not _UNSET:
            resolved_file = log_file  # explicit (path or None) wins outright
        elif stream_given:
            resolved_file = None  # explicit stream suppresses $AGENTTOOLS_LOG_FILE
        else:
            resolved_file = env_file  # nothing explicit -> env decides
        resolved_stream = None if stream is _UNSET else stream

        resolved_level = _coerce_level(level if level is not None else env_level)
        resolved_fmt = (fmt or env_fmt or "json").strip().lower()

        if force:
            for h in list(logger.handlers):
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass

        handler = _build_handler(resolved_file, resolved_stream)
        handler.setFormatter(
            PrettyFormatter() if resolved_fmt == "pretty" else JsonlFormatter()
        )
        logger.addHandler(handler)
        logger.setLevel(resolved_level)
        # Don't double-log through the stdlib root; we own this tree's output.
        logger.propagate = False
        _CONFIGURED = True
        return logger
    except Exception:  # noqa: BLE001 — configuration must never crash the caller
        # Guarantee at least a usable stderr handler so logging still works.
        logger = _root()
        if not logger.handlers:
            fallback = logging.StreamHandler(sys.stderr)
            fallback.setFormatter(JsonlFormatter())
            logger.addHandler(fallback)
            logger.setLevel(INFO)
            logger.propagate = False
        _CONFIGURED = True
        return logger


def _build_handler(
    log_file: str | os.PathLike[str] | None, stream: Any
) -> logging.Handler:
    """Build the handler, forcing 0600 on a file sink. Falls back to stderr on failure."""
    if not log_file:
        return logging.StreamHandler(stream or sys.stderr)
    try:
        path = os.fspath(log_file)
        # Create with 0600 from the start; O_APPEND so concurrent writers don't clobber.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # O_CREAT's mode only applies when WE create the file; an existing file (or a
            # pre-created $AGENTTOOLS_LOG_FILE) could carry broader perms. Force 0600 so the
            # privacy guarantee holds for pre-existing files too — mirror review-cli stats.py.
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
        # Hand the fd to a handler that OWNS and closes it on reset/reconfigure (the stdlib
        # StreamHandler would leak the fd until GC).
        return _OwningStreamHandler(os.fdopen(fd, "a", encoding="utf-8"))
    except OSError as exc:
        # Unwritable path / bad perms — never crash; degrade to stderr. Emit the diagnostic
        # as a JSON line so the default stream stays pure JSONL ("one JSON object per line").
        try:
            sys.stderr.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "level": "ERROR",
                        "logger": _ROOT_NAME,
                        "msg": "cannot open log file; falling back to stderr",
                        "log_file": str(log_file),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        except Exception:  # noqa: BLE001 — diagnostic must never crash configuration
            pass
        return logging.StreamHandler(stream or sys.stderr)


def get_logger(name: str | None = None) -> StructuredLogger:
    """Return a per-module :class:`StructuredLogger`, auto-configuring once from env.

    ``name`` is typically ``__name__``; it becomes a child of the shared ``agenttools``
    tree (``agenttools.<name>``) so one configuration governs every module's output. A
    plain ``get_logger()`` returns the root logger of the tree.
    """
    if not _CONFIGURED:
        configure(force=False)
    if not name or name == _ROOT_NAME:
        child = _root()
    else:
        child = logging.getLogger(f"{_ROOT_NAME}.{name}")
    return StructuredLogger(child)


def reset() -> None:
    """Tear down the shared configuration (handlers + configured flag).

    Primarily for tests, which reconfigure per case. Best-effort; never raises.
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        logger = _root()
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
        logger.setLevel(logging.NOTSET)
        _CONFIGURED = False
