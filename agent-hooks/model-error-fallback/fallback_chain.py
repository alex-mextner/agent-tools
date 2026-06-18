#!/usr/bin/env python3
"""Pure error-count / threshold / switch state machine for the model-fallback hook.

This module is the LOAD-BEARING half of the `model-error-fallback` agent-hook: a pure,
dependency-free state machine that decides *when to switch executor* and *to what*, given
a stream of "this turn errored / succeeded" signals. It is split out from the hook script
(`model_error_fallback.py`) on purpose so the count/threshold/promote/recover logic is
unit-testable with no JSON-on-stdin, no subprocess, no harness, no disk.

The chain it walks (`<harness>:<model>`, strongest/preferred first) is the ONE definition
read by every harness — it ships in `lib/contracts/models.yaml` under `fallback_chain:`.
The default baked in here mirrors that manifest so a host that can't read the manifest
still has a correct chain, but the manifest is the source of truth; see
:func:`chain_from_manifest_steps`.

The discipline (from ROADMAP "Model-fallback skill + cross-harness hooks"):

* Classify each turn's outcome. Only a *transient model error* (rate-limit / overload /
  API 5xx) counts toward the switch — a normal failure (a test failed, the code is wrong)
  is NOT a model error and must never burn the chain. This mirrors the review-cli
  retryable-vs-fatal split (ROADMAP "review resilience").
* Count CONSECUTIVE transient errors at the current step. On the Nth (``threshold``), drop
  to the next step. A success resets the counter (the current model recovered locally).
* Within a harness the switch is a model swap (``claude:fable`` -> ``claude:opus``); across
  the harness boundary it is a re-dispatch to that harness as an executor.
* RETURN TO THE TOP when the preferred model recovers — a successful turn after a probe of
  a higher-priority step promotes back up, so a transient throttle doesn't pin the work on
  the last-resort executor forever.

Stdlib-only. No imports beyond `dataclasses`/`typing`/`re`. The hook script imports this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar, List, Mapping, Optional, Sequence, Tuple


class FallbackError(ValueError):
    """A chain/config is malformed — e.g. an empty chain, or a step missing harness/model.

    Raised loudly rather than degrading to a silent no-op: a fallback machine with no chain
    can never switch, which is exactly the wedge this whole feature exists to prevent.
    """


# ── outcome classification ───────────────────────────────────────────────────────────────
# The closed vocabulary of error SIGNALS that count toward a switch. These mirror the
# transient/retryable class from ROADMAP "review resilience — retry vs reserve-replace":
# HTTP 429/5xx and the human strings the providers actually emit. A failure that does NOT
# match these is a NORMAL failure (wrong code, failing test, refusal) and must NOT burn the
# chain — switching executor would not fix it, and would waste the reserve quota.
TRANSIENT_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b429\b",
        # 5xx transient family: 500/502/503/504 gateway-class, 520-524 (Cloudflare gateway),
        # and 529 (Anthropic overloaded). A bare numeric code still counts (e.g. "HTTP 529");
        # the error-channel split (output/message never reach the classifier) keeps an
        # incidental "502" in normal model output from triggering a false fallback.
        r"\b5(?:0[0234]|2[0-4]|29)\b",
        r"rate[\s_-]?limit",
        r"temporarily (?:limiting|unavailable)",
        r"overloaded",
        r"server (?:error|temporarily)",
        r"service unavailable",
        r"too many requests",
        r"\bquota\b",
        # "at/over/out-of/insufficient capacity" only — NOT a bare "capacity" and NOT
        # "no capacity", so a refusal like "I have no capacity to help with that" or "I
        # don't have the capacity" is NOT misread as a transient outage (that would burn the
        # chain on a non-transient failure, violating "classify first"). The kept qualifiers
        # are provider-capacity-specific; "no" is dropped because it reads as a refusal. The
        # leading \b stops "at capacity" matching inside "great capacity" (a refusal phrase).
        r"\b(?:at|over|out of|insufficient)\s+capacity",
        r"\bthrottl",  # throttle / throttled / throttling
    )
)


def is_transient_model_error(text: str) -> bool:
    """True if ``text`` reads as a TRANSIENT model error worth falling back on.

    Transient == the class a different provider's quota could serve right now: rate-limit /
    overload / 5xx. A plain test failure or a wrong-answer is NOT transient (switching
    executor wouldn't help), so this returns False for those — the chain is preserved for
    real provider outages only.
    """
    if not text:
        return False
    return any(p.search(text) for p in TRANSIENT_PATTERNS)


# ── the chain ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainStep:
    """One executor on the fallback chain: a harness boundary + a concrete model id.

    ``harness`` is the EXECUTOR (claude / oc / codex / pi) — equal to the running harness ->
    an in-process model swap; different -> a re-dispatch to that harness. ``model`` is the
    id passed to that harness's own selector. ``notation`` is the ``<harness>:<model>``
    label surfaced when this step goes active.
    """

    harness: str
    model: str
    notation: str = ""

    @property
    def label(self) -> str:
        """The display label — explicit ``notation`` if given, else ``harness:model``."""
        return self.notation or f"{self.harness}:{self.model}"


# The default chain — mirrors lib/contracts/models.yaml `fallback_chain:`. The manifest is
# the source of truth (so cc/codex/oc/pi all read ONE list); this baked-in copy is the
# offline fallback for a host that can't load the manifest, and the thing tests pin against.
DEFAULT_CHAIN: Tuple[ChainStep, ...] = (
    ChainStep("claude", "fable", "claude:fable"),
    ChainStep("claude", "opus", "claude:opus"),
    ChainStep("oc", "GLM-5.2", "oc:GLM-5.2"),
    ChainStep("codex", "gpt5.5", "codex:gpt5.5"),
)


def chain_from_manifest_steps(steps: Sequence[Mapping[str, str]]) -> Tuple[ChainStep, ...]:
    """Build a chain tuple from a manifest's ``fallback_chain:`` list of mappings.

    Each mapping is ``{harness, model[, notation]}`` (the shape in models.yaml). The LIST
    ORDER is the priority (strongest/preferred first) — exactly how the manifest is written.
    A step missing ``harness`` or ``model`` is a :class:`FallbackError` (a silently-dropped
    step would shorten the chain and could strand the work one executor early).
    """
    if not steps:
        raise FallbackError("fallback_chain is empty — at least one step is required")
    out: List[ChainStep] = []
    for raw in steps:
        harness = str(raw.get("harness", "")).strip()
        model = str(raw.get("model", "")).strip()
        if not harness:
            raise FallbackError(f"fallback step missing 'harness': {dict(raw)!r}")
        if not model:
            raise FallbackError(f"fallback step missing 'model': {dict(raw)!r}")
        notation = str(raw.get("notation", "")).strip()
        out.append(ChainStep(harness=harness, model=model, notation=notation))
    return tuple(out)


# ── the state machine ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwitchDecision:
    """What the host must do after one observed outcome.

    ``switched`` is True iff the active step changed this observation. ``kind`` is the
    DIRECTION of the change: ``"none"`` (nothing changed), ``"fall"`` (dropped one step down
    the chain on repeated errors), or ``"recover"`` (promoted one step back toward the top).
    ``from_step`` / ``to_step`` are the chain positions before/after; ``active`` is the step
    now in force; ``reason`` is a one-line human explanation to surface.

    Whether the change is an in-process MODEL SWAP or a cross-harness RE-DISPATCH is a
    separate axis — :attr:`crosses_harness` — computed from the harnesses of ``from_step`` /
    ``to_step``, NOT from ``kind``. A recovery can cross a harness too (e.g. ``codex:gpt5.5``
    -> ``oc:GLM-5.2``), so the boundary test must look at the steps, not the direction.
    """

    switched: bool
    kind: str
    active: ChainStep
    from_step: Optional[ChainStep] = None
    to_step: Optional[ChainStep] = None
    reason: str = ""
    # True only when the chain is EXHAUSTED: a transient error hit the threshold on the
    # last-resort executor with nowhere left to fall. A machine-readable signal so a host can
    # detect "we're out of fallbacks, fail loud" structurally, not by grepping the reason.
    exhausted: bool = False

    @property
    def crosses_harness(self) -> bool:
        """True when the active executor moved to a DIFFERENT harness this observation.

        Computed from ``from_step.harness != to_step.harness`` in EITHER direction — a fall
        and a recovery both cross the boundary when the two steps live in different harnesses,
        and a host that re-dispatches on this flag must do so for a cross-harness recovery
        too, not only a fall. False when nothing switched or the switch stayed in-harness.
        """
        if not self.switched or self.from_step is None or self.to_step is None:
            return False
        return self.from_step.harness != self.to_step.harness


@dataclass
class FallbackState:
    """The mutable error-count / position state for ONE unit of work.

    Counts CONSECUTIVE transient model errors at the current chain position; on the
    ``threshold``-th it advances to the next step. A non-error (success) resets the counter
    and, if we are below the top of the chain, PROMOTES one step back toward the preferred
    model (return-to-top on recovery). A normal (non-transient) failure is a no-op for the
    chain — it neither advances nor resets, because it is unrelated to provider health.

    Persisted by the host across turns of the same unit of work (keyed by task/event id);
    pure here — :meth:`observe` returns a :class:`SwitchDecision`, the host does the I/O.
    """

    chain: Tuple[ChainStep, ...] = DEFAULT_CHAIN
    threshold: int = 3
    index: int = 0
    consecutive_errors: int = 0
    history: List[str] = field(default_factory=list)
    # The history is a debug breadcrumb, persisted in every snapshot. It is bounded to the
    # last MAX_HISTORY entries so a long session that periodically throttles (fall -> recover
    # -> fall -> ...) can't grow the snapshot file without bound — only the recent transitions
    # matter for diagnosis. ClassVar so it's a class constant, NOT an __init__ parameter (a
    # per-instance MAX_HISTORY=0 must not be able to silently disable trimming).
    MAX_HISTORY: ClassVar[int] = 50

    def _record(self, entry: str) -> None:
        """Append a transition to the bounded history (drops the oldest past MAX_HISTORY)."""
        self.history.append(entry)
        if len(self.history) > self.MAX_HISTORY:
            del self.history[: len(self.history) - self.MAX_HISTORY]

    def __post_init__(self) -> None:
        if not self.chain:
            raise FallbackError("FallbackState needs a non-empty chain")
        if self.threshold < 1:
            raise FallbackError(f"threshold must be >= 1, got {self.threshold}")
        if not (0 <= self.index < len(self.chain)):
            raise FallbackError(
                f"index {self.index} out of range for a chain of {len(self.chain)}"
            )

    @property
    def active(self) -> ChainStep:
        """The chain step currently in force."""
        return self.chain[self.index]

    @property
    def at_last_resort(self) -> bool:
        """True when the active step is the last in the chain (nowhere left to fall)."""
        return self.index >= len(self.chain) - 1

    def _decision(
        self,
        *,
        switched: bool,
        kind: str,
        from_step: Optional[ChainStep],
        to_step: Optional[ChainStep],
        reason: str,
        exhausted: bool = False,
    ) -> SwitchDecision:
        return SwitchDecision(
            switched=switched,
            kind=kind,
            active=self.active,
            from_step=from_step,
            to_step=to_step,
            reason=reason,
            exhausted=exhausted,
        )

    def observe(self, *, error: bool, detail: str = "") -> SwitchDecision:
        """Record one turn's outcome; return what the host should do.

        ``error=True`` with a TRANSIENT ``detail`` (or no detail) increments the consecutive
        count and, on the ``threshold``-th, advances one step. ``error=True`` with a
        NON-transient ``detail`` is ignored for the chain (a normal failure). ``error=False``
        (a success) resets the count and promotes back toward the top if we had fallen.
        """
        if error:
            return self._on_error(detail)
        return self._on_success()

    def _on_error(self, detail: str) -> SwitchDecision:
        # Only a transient model error counts. A detail that is present but NOT transient is a
        # normal failure (wrong code / failing test); it must not burn the chain. An ERROR
        # signal with no detail is treated as transient (the host already classified it as a
        # model error before calling with error=True).
        if detail and not is_transient_model_error(detail):
            return self._decision(
                switched=False,
                kind="none",
                from_step=None,
                to_step=None,
                reason="non-transient failure ignored for fallback "
                f"(still on {self.active.label})",
            )

        self.consecutive_errors += 1
        if self.consecutive_errors < self.threshold:
            return self._decision(
                switched=False,
                kind="none",
                from_step=None,
                to_step=None,
                reason=(
                    f"{self.consecutive_errors}/{self.threshold} transient errors on "
                    f"{self.active.label} — retrying same model before switching"
                ),
            )

        # Threshold reached. Advance — unless already at the last resort, where there is
        # nowhere left to fall; we surface that loudly. We record the exhausted marker ONCE
        # (not on every subsequent error) so a stuck task can't grow the history/snapshot
        # without bound, and we CAP consecutive_errors at the threshold so the persisted int
        # doesn't climb forever on a task stranded at the last resort.
        if self.at_last_resort:
            self.consecutive_errors = self.threshold
            marker = f"exhausted at {self.active.label}"
            if not self.history or self.history[-1] != marker:
                self._record(marker)
            return self._decision(
                switched=False,
                kind="none",
                from_step=None,
                to_step=None,
                exhausted=True,
                reason=(
                    f"chain EXHAUSTED — {self.consecutive_errors} transient errors on the "
                    f"last-resort executor {self.active.label}; no further fallback exists "
                    "(fail loud, do not pretend success)"
                ),
            )

        from_step = self.active
        self.index += 1
        self.consecutive_errors = 0
        to_step = self.active
        crosses = from_step.harness != to_step.harness
        verb = (
            "CROSS-HARNESS re-dispatch to a different executor"
            if crosses
            else "in-harness model swap"
        )
        self._record(
            f"{from_step.label} -> {to_step.label} ({'redispatch' if crosses else 'swap'})"
        )
        return self._decision(
            switched=True,
            kind="fall",
            from_step=from_step,
            to_step=to_step,
            reason=(
                f"{self.threshold} transient errors on {from_step.label}: {verb} -> "
                f"{to_step.label} is now the active executor"
            ),
        )

    def _on_success(self) -> SwitchDecision:
        # A success means the current model is healthy right now. Reset the consecutive
        # count. If we had fallen below the top, PROMOTE one step back toward the preferred
        # model — the return-to-top discipline, applied incrementally so each recovery probes
        # the next-higher step rather than jumping blind to the very top.
        self.consecutive_errors = 0
        if self.index == 0:
            return self._decision(
                switched=False,
                kind="none",
                from_step=None,
                to_step=None,
                reason=f"success on the preferred {self.active.label} — staying at the top",
            )
        from_step = self.active
        self.index -= 1
        to_step = self.active
        self._record(f"recover {from_step.label} -> {to_step.label}")
        return self._decision(
            switched=True,
            kind="recover",
            from_step=from_step,
            to_step=to_step,
            reason=(
                f"recovered on {from_step.label}: promoting back toward the preferred model "
                f"-> {to_step.label} is now active"
            ),
        )

    def snapshot(self) -> dict:
        """A JSON-serialisable view of the state, for the host to persist across turns."""
        return {
            "index": self.index,
            "consecutive_errors": self.consecutive_errors,
            "threshold": self.threshold,
            "active": self.active.label,
            "history": list(self.history),
        }


def state_from_snapshot(
    snap: Mapping[str, object],
    chain: Sequence[ChainStep] = DEFAULT_CHAIN,
) -> FallbackState:
    """Rebuild a :class:`FallbackState` from a :meth:`FallbackState.snapshot` dict.

    The host persists the snapshot (keyed by unit-of-work id) between turns and rehydrates
    it here on the next turn. Handling of each field:

    * ``index`` (the load-bearing position): a **too-large** value is CLAMPED to the last
      step of ``chain`` — the legitimate "the manifest chain got shorter between turns" case,
      where the saved position should fall back to the new last step, not reset to the top
      (which would re-burn the chain). A **negative** value is genuine corruption and raises
      :class:`FallbackError`. A non-integer value raises ``ValueError`` from ``int()`` (the
      hook catches it and resets to the top — there is no sensible position to recover).
    * ``threshold`` is NON-load-bearing on load: the host always re-applies the live env
      threshold after rehydrating, so a missing/garbage saved value falls back to the default
      here rather than failing the whole load over a field that's about to be overwritten.
    """
    # A snapshot that isn't even a mapping (a bare scalar/array landed in the state file) is
    # corruption — raise FallbackError so the host's reset-to-top path handles it, rather than
    # letting snap.get(...) raise an unguarded AttributeError out of the fail-open hook.
    if not isinstance(snap, Mapping):
        raise FallbackError(f"corrupt snapshot: not a mapping ({type(snap).__name__})")
    chain_t = tuple(chain)
    # A non-int index (e.g. a list/object) is corruption too — normalise the TypeError into a
    # FallbackError so it joins the same reset-to-top handling, not an unguarded crash.
    try:
        raw_index = int(snap.get("index", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise FallbackError(f"corrupt snapshot: non-integer index ({exc})") from exc
    if raw_index < 0:
        raise FallbackError(f"corrupt snapshot: negative index {raw_index}")
    # Clamp a too-large index to the last valid step (manifest chain shortened between turns).
    index = min(raw_index, len(chain_t) - 1)
    # threshold is re-applied live by the host; a garbage saved value defaults rather than
    # crashing the load (a too-small value would also fail FallbackState.__post_init__).
    try:
        threshold = int(snap.get("threshold", 3))
    except (TypeError, ValueError):
        threshold = 3
    if threshold < 1:
        threshold = 3
    # consecutive_errors degrades softly like index/threshold: a garbage value defaults to 0
    # rather than raising and forcing a full reset that would throw away the valid index.
    try:
        consecutive = int(snap.get("consecutive_errors", 0) or 0)
    except (TypeError, ValueError):
        consecutive = 0
    history_raw = snap.get("history") or []
    # A str/bytes is technically a Sequence; exclude it so a stringified history isn't split
    # into characters. Only a real list/tuple is read as the history.
    history = (
        [str(h) for h in history_raw]
        if isinstance(history_raw, (list, tuple))
        else []
    )
    state = FallbackState(
        chain=chain_t,
        threshold=threshold,
        index=index,
        consecutive_errors=max(0, consecutive),
        history=history,
    )
    return state
