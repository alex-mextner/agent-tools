"""transport — the DEFERRED half of the providers abstraction, owned by this tool.

The shared ``agenttools_providers`` CORE is deliberately network-free (see its README,
"Deferred"): it orders seats and resolves keys but never reaches a model. research-cli
supplies the missing transport here:

  - an availability PREDICATE for the CORE's Board (``board.split(predicate=...)``) — "is
    this seat reachable on this machine?" (a key resolvable via the CORE key cascade);
  - a live CALL that asks a resolved model a question and returns its text.

Both are behind a tiny :class:`Transport` protocol so the panel :mod:`engine` is pure
with respect to the network: production wires :class:`SubprocessTransport`; tests inject
a deterministic fake (see ``tests/test_research_cli.py``). This is exactly the seam the
CORE's README calls "a thin transports module that imports this CORE and adds the
network".

HEAVY IMPORTS ARE LAZY (lazy-heavy-imports skill): nothing here imports ``subprocess`` /
``shutil`` at module top, so ``research --help`` / ``--version`` and the test suite stay
fast and import-clean. The live backend imports them inside the call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Protocol, runtime_checkable

from .providers import BoardSeat, ModelEntry, key_cascade_for


@dataclass(frozen=True)
class SeatAnswer:
    """One seat's answer to the research question (or a failure).

    ``ok`` False means the seat could not be reached or the call failed; ``error`` then
    carries why. The synthesis step keeps failures visible rather than silently dropping a
    seat — a panel that quietly shrank is worse than one that says who was absent.
    """

    seat: BoardSeat
    model_id: str
    provider: str
    ok: bool
    text: str = ""
    error: str = ""


@runtime_checkable
class Transport(Protocol):
    """The network seam: reachability + a live call. Injectable for testability."""

    def is_reachable(self, entry: ModelEntry) -> bool:
        """Whether a resolved model can actually be called on this machine right now."""
        ...

    def ask(self, entry: ModelEntry, *, question: str, lens: str, timeout: float) -> str:
        """Ask ``entry`` the research ``question`` through ``lens``; return its text.

        Raises :class:`TransportError` on any failure (no key, binary missing, non-zero
        exit, timeout). The engine catches it and records a failed :class:`SeatAnswer`.
        """
        ...


class TransportError(RuntimeError):
    """A seat could not be reached or its call failed — carries an actionable message."""


# --- A fake transport, the testable default for offline/CI ------------------------------


@dataclass
class StubTransport:
    """A deterministic, network-free transport for tests and offline demos.

    ``reachable`` decides ``is_reachable`` (default: everything reachable). ``answers``
    maps a model id to canned text; an id absent from it yields a generic stub answer so
    the engine always has something to synthesize. ``calls`` records every ``ask`` for
    assertions. No subprocess, no network — the whole panel engine runs under it.
    """

    answers: Mapping[str, str] = field(default_factory=dict)
    reachable: Callable[[ModelEntry], bool] = lambda _entry: True
    calls: list = field(default_factory=list)

    def is_reachable(self, entry: ModelEntry) -> bool:
        return bool(self.reachable(entry))

    def ask(self, entry: ModelEntry, *, question: str, lens: str, timeout: float) -> str:
        self.calls.append({"model": entry.id, "lens": lens, "question": question})
        if entry.id in self.answers:
            return self.answers[entry.id]
        return f"[{lens or entry.id}] stub answer to: {question}"


# --- The live backend: key cascade for reachability + a provider CLI/HTTP call ----------


@dataclass
class SubprocessTransport:
    """The production transport: reachability via the CORE key cascade, call via a backend.

    REACHABILITY reuses the shared key cascade verbatim — a seat is reachable iff its
    provider has a key resolvable via ``KeyCascade`` (env beats ``.env`` files, name
    precedence beats file order). The CORE owns that precedence; this just asks it.

    THE LIVE CALL is intentionally a thin, swappable shell-out. A full multi-provider
    transport (the ``oc:`` router, api|cli mode selection, response parsing, sidecar
    logging) is the phased follow-up the README tracks; the MVP shells to one configurable
    command template so the panel end-to-end path is real, not mocked, when a key exists.
    The template gets ``{question}`` and ``{lens}`` and is expected to print the answer to
    stdout. With no template configured, a live call raises :class:`TransportError` (the
    seat is recorded as unreachable rather than crashing the run).
    """

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # A shell command template, e.g. "opencode run --model {model} {question}". Read from
    # RESEARCH_BACKEND_CMD when not passed explicitly. None => no live backend wired.
    command_template: Optional[str] = None
    # `.env` files the key cascade falls back to (after env vars). Defaults to the
    # conventional `.env` in the cwd, so the documented "env beats .env files" cascade
    # actually reads a file; pass () to disable file fallback entirely.
    dotenv_files: tuple = (".env",)

    def __post_init__(self) -> None:
        if self.command_template is None:
            self.command_template = self.env.get("RESEARCH_BACKEND_CMD") or None

    def is_reachable(self, entry: ModelEntry) -> bool:
        cascade = key_cascade_for(entry.provider)
        if cascade is None:
            return False
        from pathlib import Path  # local: keep module-top import-clean

        files = tuple(Path(p) for p in self.dotenv_files)
        cascade = type(cascade)(names=cascade.names, files=files)
        return bool(cascade.resolve(env=self.env))

    def ask(self, entry: ModelEntry, *, question: str, lens: str, timeout: float) -> str:
        if not self.command_template:
            raise TransportError(
                "no live research backend configured. Set RESEARCH_BACKEND_CMD to a "
                "command template (it receives {model} {lens} {question} and must print "
                "the answer), or run with --offline to use the stub transport."
            )
        import shlex
        import subprocess  # lazy heavy import

        # Substitute ONLY our three known placeholders, so a template that also contains
        # shell `${VAR}` references (a common, idiomatic case) is left intact — str.format
        # would choke on `{VAR}` and `${VAR}` as unknown fields / raise KeyError.
        cmd = self.command_template
        for token, value in (
            ("{model}", entry.id),
            ("{lens}", lens or "analyst"),
            ("{question}", question),
        ):
            cmd = cmd.replace(token, shlex.quote(value))
        try:
            proc = subprocess.run(  # noqa: S602 — template is operator-supplied config
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"{entry.id}: timed out after {timeout:g}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            why = tail[-1] if tail else f"exit {proc.returncode}"
            raise TransportError(f"{entry.id}: backend failed: {why}")
        out = (proc.stdout or "").strip()
        if not out:
            raise TransportError(f"{entry.id}: backend returned no output")
        return out
