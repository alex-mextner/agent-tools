"""engine — the single-round multi-provider research pass (the MVP).

THE PASS (one round):
  1. Resolve the failover Board against the shared registry (CORE ``resolve_role``).
  2. Use the injected transport's reachability as the CORE Board's availability PREDICATE,
     so ``board.split(pool_size, predicate)`` returns the top-N reachable seats + a reserve
     — the CORE owns the ordering, this owns "is it reachable".
  3. Ask each pooled seat the question through its lens (the transport's live call). If a
     pooled seat FAILS at call time, promote the next reserve seat (mid-run failover) — the
     CORE gave the order, the engine runs it (the CORE README's "the tool runs it").
  4. Synthesize the collected answers into one note.

PHASED REST (tracked in the README): multi-round follow-ups, cross-examination between
seats, and citation/source verification. The MVP stops at one round + a structured
synthesis, which is a genuinely useful multi-provider answer, not a stub.

This module is PURE with respect to the network — every model touch goes through the
injected :class:`~research_cli.transport.Transport`, so the whole pass is unit-tested with
a stub transport, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .providers import (
    Board,
    BoardSeat,
    ProviderError,
    Registry,
    load_research_registry,
    research_board,
    resolve_seat,
)
from .transport import SeatAnswer, Transport, TransportError


@dataclass(frozen=True)
class ResearchResult:
    """The outcome of a research run: every seat's answer + the synthesized note."""

    question: str
    # A tuple, not a list: the result is frozen, so its contents are immutable too (a
    # mutable list inside a frozen dataclass would let a caller append after the fact).
    answers: Tuple[SeatAnswer, ...]
    synthesis: str

    @property
    def answered(self) -> List[SeatAnswer]:
        return [a for a in self.answers if a.ok]

    @property
    def failed(self) -> List[SeatAnswer]:
        return [a for a in self.answers if not a.ok]


@dataclass
class ResearchEngine:
    """Run a single-round multi-provider research pass over a question.

    The registry and board default to the ecosystem's shared manifest + the default
    research board; both are injectable for tests and tool config. The transport is the
    network seam (always injected — there is no implicit default, so a test can never
    accidentally hit the wire).
    """

    transport: Transport
    registry: Optional[Registry] = None
    board: Optional[Board] = None
    pool_size: int = 3
    timeout: float = 120.0
    manifest_path: Optional[Path] = field(default=None)
    # Set per-run by run() so the failover loop in run() / _ask_seat need not thread the
    # question through every call. A run is single-threaded, so a per-instance slot is safe.
    _current_question: str = field(default="", repr=False)

    def _registry(self) -> Registry:
        if self.registry is not None:
            return self.registry
        self.registry = load_research_registry(self.manifest_path)
        return self.registry

    def _board(self) -> Board:
        return self.board if self.board is not None else research_board()

    def _reachable(self, registry: Registry):
        """An availability predicate for the CORE Board: a seat is reachable iff it
        resolves to a concrete model AND the transport can reach that model. An unresolved
        seat (bad role) is unreachable, not a crash — the run uses whoever IS reachable.
        """

        def predicate(seat: BoardSeat) -> bool:
            try:
                entry = resolve_seat(registry, seat)
            except ProviderError:
                return False
            return self.transport.is_reachable(entry)

        return predicate

    def run(self, question: str) -> ResearchResult:
        question = question.strip()
        if not question:
            raise ValueError("research question must be non-empty")

        self._current_question = question
        registry = self._registry()
        board = self._board()
        predicate = self._reachable(registry)
        pool, reserve = board.split(self.pool_size, predicate)
        reserve = list(reserve)

        answers: List[SeatAnswer] = []
        for seat in pool:
            answer = self._ask_seat(registry, seat)
            # Mid-run failover: a pooled seat that fails at CALL time (passed reachability
            # but the live call errored) is backfilled by the next reachable reserve seat,
            # so a transient failure does not silently shrink the panel below pool_size.
            while not answer.ok and reserve:
                answers.append(answer)  # keep the failure visible
                seat = reserve.pop(0)
                answer = self._ask_seat(registry, seat)
            answers.append(answer)

        synthesis = synthesize(question, answers)
        return ResearchResult(
            question=question, answers=tuple(answers), synthesis=synthesis
        )

    def _ask_seat(self, registry: Registry, seat: BoardSeat) -> SeatAnswer:
        try:
            entry = resolve_seat(registry, seat)
        except ProviderError as exc:
            return SeatAnswer(
                seat=seat, model_id=seat.model, provider="", ok=False, error=str(exc)
            )
        try:
            text = self.transport.ask(
                entry,
                question=self._current_question,
                lens=seat.role,
                timeout=self.timeout,
            )
        except Exception as exc:
            # The transport is an injected boundary: a TransportError is expected, but a
            # live/custom backend can also raise OSError (missing binary), a parse error,
            # etc. ANY failure of one seat must be RECORDED, not abort the whole panel —
            # the run continues with the other seats and the reserve backfill.
            return SeatAnswer(
                seat=seat,
                model_id=entry.id,
                provider=entry.provider,
                ok=False,
                error=f"{type(exc).__name__}: {exc}" if not isinstance(exc, TransportError)
                else str(exc),
            )
        return SeatAnswer(
            seat=seat,
            model_id=entry.id,
            provider=entry.provider,
            ok=True,
            text=text.strip(),
        )


def synthesize(question: str, answers: List[SeatAnswer]) -> str:
    """Combine the panel's answers into one Markdown research note.

    MVP synthesis is DETERMINISTIC and offline: it lays out every seat's answer attributed
    to its model + lens, then a short "panel" footer noting agreement breadth and any seat
    that failed. A model-driven synthesis (asking a strong seat to reconcile the answers)
    is the obvious phase-2 upgrade; keeping the MVP deterministic means the output is
    testable and never itself a hallucination.
    """
    answered = [a for a in answers if a.ok]
    failed = [a for a in answers if not a.ok]

    lines: List[str] = [f"# Research: {question}", ""]
    if not answered:
        lines.append("_No seat produced an answer._")
    for a in answered:
        label = a.seat.display or a.model_id
        lens = f" · {a.seat.role}" if a.seat.role else ""
        lines.append(f"## {label}{lens}  ({a.model_id})")
        lines.append("")
        lines.append(a.text or "_(empty)_")
        lines.append("")

    lines.append("---")
    n_ok = len(answered)
    lines.append(
        f"**Panel:** {n_ok} model"
        + ("s" if n_ok != 1 else "")
        + " answered"
        + (f"; {len(failed)} unavailable" if failed else "")
        + "."
    )
    for a in failed:
        label = a.seat.display or a.model_id
        lines.append(f"- _{label} unavailable: {a.error}_")
    return "\n".join(lines).rstrip() + "\n"


def _seat_payload(answer: SeatAnswer) -> Dict[str, Any]:
    """One seat's answer as a JSON-serializable dict (the per-seat shape of render_json).

    Carries everything a downstream consumer needs without scraping Markdown: the seat's
    display + lens, the concrete model + provider it resolved to, and the outcome (ok with
    text, or not-ok with the failure reason).
    """
    return {
        "display": answer.seat.display or answer.model_id,
        "lens": answer.seat.role,
        "model": answer.model_id,
        "provider": answer.provider,
        "ok": answer.ok,
        "text": answer.text,
        "error": answer.error,
    }


def render_json(question: str, answers: List[SeatAnswer]) -> str:
    """Serialize a panel run as a stable JSON object — the machine-readable counterpart of
    :func:`synthesize`.

    Same inputs as ``synthesize`` (the question + every seat's answer), so a caller renders
    EITHER the human note OR this structured object from one run. The shape is the contract
    consumers depend on: a top-level ``question`` + ``answered``/``failed`` counts + a
    ``seats`` array (one :func:`_seat_payload` per seat, in board order). Deterministic and
    network-free, exactly like the synthesis — it never itself calls a model.
    """
    answered = [a for a in answers if a.ok]
    failed = [a for a in answers if not a.ok]
    payload: Dict[str, Any] = {
        "question": question,
        "answered": len(answered),
        "failed": len(failed),
        "seats": [_seat_payload(a) for a in answers],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
