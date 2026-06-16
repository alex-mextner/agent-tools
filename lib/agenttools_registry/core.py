"""agenttools_registry.core — the shared TRUST KERNEL + registry primitives.

WHAT THIS IS
------------
A payload-agnostic registry of *entries* (each with a name + an executable/loadable
``cmd`` path + an arbitrary ``invocation`` digest input) and a single ``trust_state``
decision function. It is the one place that owns the algorithm both ecosystem consumers
re-implement today:

  * review-cli ``reviewlib/features/visual/registry.py`` — the per-project visual-module
    registry (Python): discover ``<project>/.review/visual-modules.json`` → trust-gate →
    ``importlib`` load. Pins ``entry_sha256`` + ``activates_on``.
  * tg-cli ``features/hooks/runner.ts`` + ``types.ts`` — the ``agents-hooks/v1`` trust
    gate (TypeScript): discover dropped-in hook descriptors → trust-gate → ``exec``. Pins
    ``cmd_sha256`` + ``invocation_sha256`` (cmd+args+timeout) + ``on_error``.

The shared-lib design doc (``docs/specs/2026-06-15-shared-lib-architecture.md`` §1/§5)
calls these "the SAME ALGORITHM implemented twice in two languages" and asks for one
trust kernel. This module is that kernel, generalized so neither consumer's payload shape
leaks in: an ``Entry`` carries a ``cmd`` path, an optional pre-computed ``invocation``
string, and an opaque ``meta`` dict the consumer owns.

THE ALGORITHM (identical in both originals)
-------------------------------------------
1. **Trust-by-default.** With the guard OFF (the common case — your own repo, your own
   dropped-in modules) every entry whose ``cmd`` exists is ``trusted-default`` and runs
   with NO pin and NO ceremony. This is the whole point: reviewing/​hooking your OWN code
   must not nag.
2. **Opt-in guard.** A truthy ``guard`` (driven by ``REVIEW_UNTRUSTED_MODULES=1`` /
   ``AGENTS_HOOKS_TRUST=1`` in the consumers) re-engages a TOFU (trust-on-first-use)
   quarantine for the rare untrusted-input case (an external PR, a cloned stranger's
   repo). Under the guard:
     - a never-seen entry is ``quarantined-new`` (inert — treated as ABSENT, never a
       block — a loud banner tells the user how to trust it);
     - an entry whose ``cmd`` bytes (``cmd_sha256``) or whose invocation digest
       (``invocation_sha256``) changed since the pin is ``quarantined-changed``;
     - a matching pin yields ``trusted``.
3. **``auto`` escape hatch.** ``auto=True`` (``…=auto`` in the consumers) bypasses the
   pins under the guard — the batch/agent escape hatch. No effect when the guard is off
   (already trusted).
4. **Missing cmd** is ``untrusted-missing-cmd`` regardless of the guard — there is
   nothing to run.
5. **Audit.** Every decision is one append-only JSON line (``audit_jsonl``). Auditing is
   best-effort and must NEVER break the caller.

A ``TrustState`` is ``runnable`` iff it is ``trusted-default`` / ``trusted`` / ``auto``.
``quarantined-*`` and ``untrusted-missing-cmd`` are inert: do not run, do not block.

STDLIB ONLY at import time (``hashlib`` / ``json`` / ``os`` / ``time`` / ``pathlib`` /
``dataclasses`` / ``enum``). No heavy deps; no network; no sleeps. ``importlib`` (the only
thing close to "heavy") is imported lazily inside :func:`load_python_entry`, the optional
Python-loader convenience — the trust decision itself never imports it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# --------------------------------------------------------------------------------------
# Trust states. The exact set both originals use (review's _trust_state_for strings + tg's
# TrustState union), unified. The string values match tg-cli's TS union verbatim so a
# shared audit.jsonl reads the same across the Python and TS hosts.
# --------------------------------------------------------------------------------------
class TrustState(str, Enum):
    TRUSTED_DEFAULT = "trusted-default"  # guard off → trust-by-default, no pin
    TRUSTED = "trusted"  # guard on, sha + invocation matched a pin
    AUTO = "auto"  # guard on, pins bypassed via the =auto escape hatch
    QUARANTINED_NEW = "quarantined-new"  # guard on, never pinned — inert + banner
    QUARANTINED_CHANGED = "quarantined-changed"  # guard on, cmd/invocation changed — inert
    UNTRUSTED_MISSING_CMD = "untrusted-missing-cmd"  # cmd path does not exist — inert

    def __str__(self) -> str:  # so f"{state}" / json gives the bare value, not Enum repr
        return self.value


#: States under which an entry is allowed to run.
_RUNNABLE = frozenset(
    {TrustState.TRUSTED_DEFAULT, TrustState.TRUSTED, TrustState.AUTO}
)
#: States under which an entry is inert (absent — never a block).
_QUARANTINED = frozenset(
    {
        TrustState.QUARANTINED_NEW,
        TrustState.QUARANTINED_CHANGED,
        TrustState.UNTRUSTED_MISSING_CMD,
    }
)


def is_runnable(state: TrustState) -> bool:
    """True iff ``state`` permits the entry to run (trusted/​trusted-default/​auto)."""
    return state in _RUNNABLE


def is_quarantined(state: TrustState) -> bool:
    """True iff ``state`` makes the entry inert (quarantined / missing cmd)."""
    return state in _QUARANTINED


# --------------------------------------------------------------------------------------
# Entry + pin shapes. An Entry is the generalized, payload-agnostic union of review's
# ModuleSpec and tg's HookDescriptor: a name, a key (for pin lookup / audit correlation),
# the executable/loadable ``cmd``, and the invocation-digest inputs.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One discovered registry entry, before trust is evaluated.

    ``name``        human / store key (review's module name, tg's ``id.point``).
    ``cmd``         absolute path to the executable / loadable file whose BYTES are pinned
                    (review's ``entry_path``, tg's ``cmd``). Its existence + content drive
                    the trust decision.
    ``args``        argv tokens that, with ``cmd`` + ``timeout_ms``, form the invocation
                    digest (tg). For review-style entries with no argv this is empty and
                    the digest collapses to the cmd path + tags (see ``extra_digest``).
    ``timeout_ms``  the per-run timeout; folded into the invocation digest because a
                    fail-open gate forced to time out becomes an allow (tg's rationale).
    ``extra_digest`` any further fields that must re-quarantine on change WITHOUT touching
                    the cmd bytes — review pins ``activates_on`` here (a manifest-only tag
                    widening keeps the hash but must re-trust).
    ``meta``        opaque consumer payload (review's runtime/​description, tg's on_error).
                    The kernel never interprets it; it rides into the loaded result.
    """

    name: str
    cmd: Path
    args: tuple[str, ...] = ()
    timeout_ms: Optional[int] = None
    extra_digest: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrustPin:
    """A pinned entry in the trust store (only consulted under the guard).

    ``cmd_sha256``        the executable's bytes when trusted (catches a swapped binary).
    ``invocation_sha256`` digest of cmd + args + timeout + extra_digest (catches an args /
                          tag / timeout swap that leaves the bytes untouched). Absent in a
                          forward-compat read → treated as stale (re-trust required), per
                          tg's note.
    ``trusted_at``        unix time the pin was written (informational).
    ``meta``              opaque (tg pins ``on_error`` here — the authoritative policy the
                          descriptor only proposes).
    """

    cmd_sha256: str
    invocation_sha256: Optional[str] = None
    trusted_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrustDecision:
    """The verdict for one entry: its state, a human reason, and the freshly computed
    digests (so the caller can pin them on a ``trust`` verb / write them to audit)."""

    name: str
    state: TrustState
    reason: str
    cmd_sha256: Optional[str]  # None when the cmd is unreadable / missing
    invocation_sha256: str
    runnable: bool
    pin_meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Digests. NUL-separated, matching tg's invocationDigest byte-for-byte so a Python and a
# TS host computing the digest of the same invocation agree.
# --------------------------------------------------------------------------------------
def sha256_file(path: Path) -> Optional[str]:
    """Hex sha256 of a file's bytes, or None if it can't be read (missing / unreadable).

    None (not an exception) is the signal for ``untrusted-missing-cmd`` — mirrors tg's
    ``sha256(path) -> string | null``."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def sha256_str(s: str) -> str:
    """Hex sha256 of a string's UTF-8 bytes."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def invocation_digest(
    cmd: str | os.PathLike[str],
    args: Iterable[str] = (),
    timeout_ms: Optional[int] = None,
    extra: Iterable[str] = (),
) -> str:
    """Digest the FULL invocation so any change to WHAT runs (or how long, or under which
    activation tags) requires re-trust even when the executable bytes are unchanged.

    The layout matches tg-cli's ``invocationDigest`` for the ``cmd``/``args``/``timeout``
    prefix (NUL-joined, a ``\\0timeout=<n>`` trailer that distinguishes an unset default
    from an explicit value), then appends review-style ``extra`` tokens (e.g.
    ``activates_on``) under a separate ``\\0extra=`` marker so an empty extra is
    distinguishable from a single empty token. A NUL can't appear in argv, so the join is
    unambiguous."""
    parts = [str(cmd), *[str(a) for a in args], f"\0timeout={timeout_ms if timeout_ms is not None else ''}"]
    extra_list = [str(e) for e in extra]
    if extra_list:
        parts.append("\0extra=" + "\0".join(extra_list))
    return sha256_str("\0".join(parts))


def entry_invocation_digest(entry: Entry) -> str:
    """The invocation digest for an :class:`Entry` (its cmd + args + timeout + extra)."""
    return invocation_digest(entry.cmd, entry.args, entry.timeout_ms, entry.extra_digest)


# --------------------------------------------------------------------------------------
# The trust kernel — the single decision function. This is the generalized union of
# review's _trust_state_for and tg's resolveTrust.
# --------------------------------------------------------------------------------------
def trust_state(
    entry: Entry,
    store: "TrustStore",
    *,
    guard: bool,
    auto: bool = False,
) -> TrustDecision:
    """Resolve the trust state of ``entry`` against ``store``.

    ``guard``  the opt-in untrusted-input guard (``REVIEW_UNTRUSTED_MODULES=1`` /
               ``AGENTS_HOOKS_TRUST=1``). OFF → trust-by-default; ON → TOFU quarantine.
    ``auto``   the ``…=auto`` escape hatch — bypass pins under the guard. No effect when
               the guard is off.

    Returns a :class:`TrustDecision` carrying the freshly computed digests so the caller
    can pin them (a ``trust`` verb) and audit them without recomputing.
    """
    cmd_sha = sha256_file(entry.cmd)
    inv_sha = entry_invocation_digest(entry)

    def decide(state: TrustState, reason: str, pin_meta: Optional[dict[str, Any]] = None) -> TrustDecision:
        return TrustDecision(
            name=entry.name,
            state=state,
            reason=reason,
            cmd_sha256=cmd_sha,
            invocation_sha256=inv_sha,
            runnable=is_runnable(state),
            pin_meta=pin_meta or {},
        )

    # A missing executable means there is nothing to run — guard or not (tg parity).
    if cmd_sha is None:
        return decide(TrustState.UNTRUSTED_MISSING_CMD, f"cmd not found or unreadable: {entry.cmd}")

    # Trust-by-default: the guard is off, so skip the pin machinery entirely.
    if not guard:
        return decide(TrustState.TRUSTED_DEFAULT, "trust-by-default (untrusted-input guard off)")

    # --- Guarded (paranoid) path: legacy TOFU quarantine + sha-pin. -------------------
    if auto:
        return decide(TrustState.AUTO, "auto escape hatch (guard on, pins bypassed)")

    pin = store.get(entry.name)
    if pin is None:
        return decide(TrustState.QUARANTINED_NEW, "never trusted")
    if pin.cmd_sha256 != cmd_sha:
        return decide(TrustState.QUARANTINED_CHANGED, "cmd changed since trust, re-trust required")
    # A pin written before invocation digesting existed (forward-compat read) has no
    # invocation_sha256 → treat as stale: re-trust required. This is what stops a trusted
    # interpreter cmd from being repointed via args, or its activation tags widened.
    if pin.invocation_sha256 is None or pin.invocation_sha256 != inv_sha:
        return decide(TrustState.QUARANTINED_CHANGED, "invocation changed since trust, re-trust required")
    return decide(TrustState.TRUSTED, "trusted (sha + invocation pinned)", pin_meta=dict(pin.meta))


# --------------------------------------------------------------------------------------
# The trust store — a JSON file of {name -> TrustPin}, written 0600 (it gates arbitrary
# code execution). Mirrors review's modules-trust.json and tg's TrustStore.
# --------------------------------------------------------------------------------------
class TrustStore:
    """A name → :class:`TrustPin` mapping backed by a JSON file.

    Construct from a path with :meth:`load` (a missing / garbage file → empty store, never
    an error — both originals degrade silently). :meth:`pin` records an entry's freshly
    computed digests; :meth:`save` writes the file ``0600``.
    """

    def __init__(self, path: Optional[Path] = None, pins: Optional[dict[str, TrustPin]] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._pins: dict[str, TrustPin] = dict(pins or {})

    # -- read --------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "TrustStore":
        """Load pins from ``path``. A missing / non-JSON / non-dict file → empty store."""
        store = cls(path)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return store
        if not isinstance(data, dict):
            return store
        for name, raw in data.items():
            if not isinstance(raw, dict) or "cmd_sha256" not in raw:
                continue  # skip malformed rows rather than reject the whole store
            raw_meta = raw.get("meta")
            meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            store._pins[name] = TrustPin(
                cmd_sha256=str(raw["cmd_sha256"]),
                invocation_sha256=(str(raw["invocation_sha256"]) if raw.get("invocation_sha256") is not None else None),
                trusted_at=float(raw.get("trusted_at", 0.0) or 0.0),
                meta=meta,
            )
        return store

    def get(self, name: str) -> Optional[TrustPin]:
        return self._pins.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._pins

    def __len__(self) -> int:
        return len(self._pins)

    def names(self) -> list[str]:
        return list(self._pins)

    # -- write -------------------------------------------------------------------------
    def pin(self, decision: TrustDecision, *, meta: Optional[dict[str, Any]] = None) -> TrustPin:
        """Record (in memory) a pin from a :class:`TrustDecision`. Call :meth:`save` to
        persist. Raises if the decision has no cmd hash (a missing cmd can't be pinned)."""
        if decision.cmd_sha256 is None:
            raise ValueError(f"cannot pin {decision.name!r}: cmd is missing / unreadable")
        p = TrustPin(
            cmd_sha256=decision.cmd_sha256,
            invocation_sha256=decision.invocation_sha256,
            trusted_at=time.time(),
            meta=dict(meta or decision.pin_meta or {}),
        )
        self._pins[decision.name] = p
        return p

    def remove(self, name: str) -> bool:
        """Drop a pin (re-quarantine on next guarded run). True if it existed."""
        return self._pins.pop(name, None) is not None

    def to_dict(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, p in self._pins.items():
            row: dict[str, Any] = {"cmd_sha256": p.cmd_sha256, "trusted_at": p.trusted_at}
            if p.invocation_sha256 is not None:
                row["invocation_sha256"] = p.invocation_sha256
            if p.meta:
                row["meta"] = p.meta
            out[name] = row
        return out

    def save(self, path: Optional[Path] = None) -> None:
        """Write the store to ``path`` (or the construction path), tightened to ``0600``.

        The pins gate arbitrary code execution, so the file is chmod-ed 0600 on every
        write (matching review's ``_write_trust``). Parent dirs are created."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("TrustStore.save needs a path (none given at load or save)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# Append-only audit. One JSON line per decision, best-effort — an audit failure must NEVER
# break a verification / a send. Mirrors review's _audit and tg's AuditLine.
# --------------------------------------------------------------------------------------
def audit_decision(
    audit_path: Optional[Path],
    decision: TrustDecision,
    *,
    outcome: str,
    duration_ms: float = 0.0,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Append one audit row for a trust decision. No-op when ``audit_path`` is None.

    ``outcome`` is the consumer's resolved action for this entry, e.g. ``"loaded"`` /
    ``"absent"`` / ``"allow"`` / ``"block"`` / ``"load-failed"`` — the kernel does not
    constrain it. Best-effort: any OS error is swallowed (auditing must not break the
    caller)."""
    if audit_path is None:
        return
    row: dict[str, Any] = {
        "ts": time.time(),
        "name": decision.name,
        "trust_state": str(decision.state),
        "cmd_sha256": decision.cmd_sha256,
        "invocation_sha256": decision.invocation_sha256,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 2),
    }
    if extra:
        row.update(extra)
    try:
        ap = Path(audit_path)
        ap.parent.mkdir(parents=True, exist_ok=True)
        with ap.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# The Registry facade — discover-or-supply entries, trust-gate them all, hand back the
# runnable ones and the quarantined ones. This generalizes review's load_modules() and
# tg's runHooks() trust phase WITHOUT the payload-specific load/exec (the consumer does
# that with the runnable entries it gets back).
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GatedEntry:
    """An entry paired with its trust decision after gating."""

    entry: Entry
    decision: TrustDecision


@dataclass(frozen=True)
class GateResult:
    """The outcome of gating a batch of entries."""

    runnable: list[GatedEntry]  # trusted / trusted-default / auto — the consumer runs these
    quarantined: list[GatedEntry]  # inert — absent, never a block


class Registry:
    """A trust-gated registry over a set of entries and a trust store.

    The consumer supplies entries (already discovered from its own manifest format — the
    kernel is payload-agnostic) or a ``discover`` callable, plus the guard/​auto flags and
    optional store/​audit paths. :meth:`gate` evaluates every entry through the trust
    kernel and returns the runnable + quarantined split, auditing each decision.

    The consumer keeps ownership of WHAT an entry is and HOW it runs — the registry owns
    discovery iteration, the trust decision, and the audit row.
    """

    def __init__(
        self,
        *,
        store: Optional[TrustStore] = None,
        store_path: Optional[Path] = None,
        audit_path: Optional[Path] = None,
        guard: bool = False,
        auto: bool = False,
    ) -> None:
        if store is not None:
            self.store = store
        elif store_path is not None:
            self.store = TrustStore.load(store_path)
        else:
            self.store = TrustStore()
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self.guard = guard
        self.auto = auto

    def decide(self, entry: Entry) -> TrustDecision:
        """The trust decision for one entry (no audit, no run)."""
        return trust_state(entry, self.store, guard=self.guard, auto=self.auto)

    def gate(
        self,
        entries: Iterable[Entry],
        *,
        audit_outcome: Optional[Callable[[GatedEntry], str]] = None,
    ) -> GateResult:
        """Trust-gate every entry; return the runnable / quarantined split.

        Each decision is audited (best-effort). ``audit_outcome`` maps a gated entry to
        its audit ``outcome`` label; the default labels runnable as ``"runnable"`` and
        quarantined as ``"absent"`` (the consumer can override to ``"loaded"`` /
        ``"load-failed"`` etc. once it has actually run the entry)."""
        runnable: list[GatedEntry] = []
        quarantined: list[GatedEntry] = []
        for entry in entries:
            decision = self.decide(entry)
            ge = GatedEntry(entry=entry, decision=decision)
            if decision.runnable:
                runnable.append(ge)
            else:
                quarantined.append(ge)
            outcome = audit_outcome(ge) if audit_outcome is not None else ("runnable" if decision.runnable else "absent")
            audit_decision(self.audit_path, decision, outcome=outcome)
        return GateResult(runnable=runnable, quarantined=quarantined)

    def trust(self, entry: Entry, *, save: bool = True, meta: Optional[dict[str, Any]] = None) -> TrustDecision:
        """Pin ``entry`` (the ``trust`` verb): compute its digests and record a pin.

        Returns the decision that was pinned. With ``save=True`` (default) the store is
        persisted 0600. A missing cmd can't be pinned (raises via :meth:`TrustStore.pin`).
        Under the guard-off world this is informational — the entry already runs — but the
        pin is still written so a later guarded run trusts it without re-prompting."""
        decision = trust_state(entry, self.store, guard=True, auto=False)
        self.store.pin(decision, meta=meta or dict(entry.meta))
        if save:
            self.store.save()
        audit_decision(self.audit_path, decision, outcome="trust-pinned")
        return decision


# --------------------------------------------------------------------------------------
# Optional convenience: load a Python entry file (review's _load_entry_object generalized).
# importlib is imported LAZILY here so the trust decision path never pays for it.
# --------------------------------------------------------------------------------------
def load_python_entry(
    cmd: Path,
    *,
    attr: str = "MODULE",
    factories: tuple[str, ...] = ("module", "get_module"),
    class_name: Optional[str] = "Module",
) -> Optional[object]:
    """Import a Python entry file and return its contributed object, or None.

    Looks for a top-level ``attr`` (default ``MODULE``), then a ``factories`` factory
    (``module()`` / ``get_module()``), then a ``class_name`` class to instantiate. Any
    import / construction failure → None (a broken contributed entry must not crash the
    host). The trust gate is the CALLER's responsibility — only pass a ``cmd`` whose
    :class:`TrustDecision` was runnable.
    """
    import importlib.util  # lazy: only paid when actually loading a Python entry
    import sys
    import uuid

    mod_name = f"_agenttools_registry_entry_{uuid.uuid4().hex[:8]}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(cmd))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — a broken entry must not crash the host
        sys.modules.pop(mod_name, None)
        return None
    obj = getattr(module, attr, None)
    if obj is None:
        for factory in factories:
            fn = getattr(module, factory, None)
            if callable(fn):
                try:
                    obj = fn()
                except Exception:  # noqa: BLE001
                    obj = None
                break
    if obj is None and class_name:
        cls = getattr(module, class_name, None)
        if isinstance(cls, type):
            try:
                obj = cls()
            except Exception:  # noqa: BLE001
                obj = None
    return obj
