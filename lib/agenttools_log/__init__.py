"""agenttools_log — shared structured JSONL logging for the agent-tools ecosystem.

One JSON object per line, stdlib-only. Every record carries at least ``ts`` (ISO-8601
UTC), ``level``, ``logger`` (the module name), and ``msg``; arbitrary structured fields
ride alongside as top-level keys. It is a thin, generalized wrapper over the stdlib
``logging`` module — the same shape ``rig-cli``'s ``riglib.logging`` already emits, lifted
into one importable library so ``review-cli``, ``rig-cli``, and future Python CLIs log
identically.

Why stdlib only (no ``python-json-logger``, no ``structlog``)
------------------------------------------------------------
The ecosystem is stdlib-first by directive, and both existing consumers already do this
by hand: ``review-cli`` prints to stderr with no log dependency, ``rig-cli`` ships a
``logging.Formatter`` subclass. A custom ~one-class JSON formatter is a handful of lines,
adds zero install/import cost, and avoids a third-party version surface in every consumer.
``structlog`` would be a heavy dep for no gain here; ``python-json-logger`` is thin but
still a dependency we don't need.

Quick start
-----------
    from agenttools_log import get_logger

    log = get_logger(__name__)
    log.info("server started", port=8080, pid=1234)
    log.warn("retrying", attempt=2, url="https://...")
    try:
        ...
    except Exception:
        log.error("request failed", request_id="abc", exc_info=True)

Each call emits one line, e.g.::

    {"ts":"2026-06-15T09:00:00.123456+00:00","level":"INFO","logger":"myapp",
     "msg":"server started","port":8080,"pid":1234}

Configuration
-------------
``get_logger`` auto-configures the shared root once from the environment on first use:

* ``AGENTTOOLS_LOG_LEVEL`` — ``debug``/``info``/``warn``/``error`` (default ``info``).
* ``AGENTTOOLS_LOG_FILE``  — append JSONL to this path (created ``0600``); else stderr.
* ``AGENTTOOLS_LOG_FORMAT`` — ``json`` (default) or ``pretty`` (human-readable dev mode).

Call :func:`configure` explicitly to override the environment from code (e.g. a CLI's
``--log-file`` flag). Safe defaults throughout: a logging failure never crashes the
caller, and a file sink can never silently log to stderr with broader-than-0600 perms.
"""

from __future__ import annotations

from .core import (
    DEBUG,
    ERROR,
    INFO,
    WARNING,
    StructuredLogger,
    configure,
    get_logger,
    reset,
)

__all__ = [
    "DEBUG",
    "ERROR",
    "INFO",
    "WARNING",
    "StructuredLogger",
    "configure",
    "get_logger",
    "reset",
]

__version__ = "0.1.0"
