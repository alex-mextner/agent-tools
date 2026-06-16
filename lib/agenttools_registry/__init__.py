"""agenttools_registry — the shared TRUST KERNEL + trust-gated registry.

One importable home for the algorithm two ecosystem CLIs re-implement today: review-cli's
per-project visual-module registry (``reviewlib/features/visual/registry.py``) and tg-cli's
``agents-hooks/v1`` trust gate (``features/hooks/runner.ts`` + ``types.ts``). The shared-lib
design doc (§1/§5) names them "the SAME ALGORITHM implemented twice in two languages" and
asks for one trust kernel — this is it, generalized so neither consumer's payload leaks in.

The algorithm (trust-by-default; opt-in TOFU sha-pin guard; ``auto`` escape hatch;
inert-not-blocking quarantine; append-only audit) lives in :mod:`agenttools_registry.core`.

Quick start
-----------
    from agenttools_registry import Entry, Registry, TrustState

    reg = Registry(store_path=store, audit_path=audit, guard=False)  # trust-by-default
    entries = [Entry(name="my-mod", cmd=path_to_entry_py)]
    result = reg.gate(entries)
    for ge in result.runnable:        # trusted-default / trusted / auto
        run(ge.entry)                 # the consumer owns HOW to run it
    for ge in result.quarantined:     # inert — absent, never a block
        warn(ge.decision.reason)

Under the opt-in guard (the rare untrusted-input case) pin an entry once::

    reg = Registry(store_path=store, guard=True)
    reg.trust(entry)                  # writes a {cmd_sha256, invocation_sha256} pin 0600

STDLIB ONLY at import time. ``importlib`` is lazy inside :func:`load_python_entry`.
"""

from __future__ import annotations

from .core import (
    Entry,
    GatedEntry,
    GateResult,
    Registry,
    TrustDecision,
    TrustPin,
    TrustState,
    TrustStore,
    audit_decision,
    entry_invocation_digest,
    invocation_digest,
    is_quarantined,
    is_runnable,
    load_python_entry,
    sha256_file,
    sha256_str,
    trust_state,
)

__all__ = [
    "Entry",
    "GateResult",
    "GatedEntry",
    "Registry",
    "TrustDecision",
    "TrustPin",
    "TrustState",
    "TrustStore",
    "audit_decision",
    "entry_invocation_digest",
    "invocation_digest",
    "is_quarantined",
    "is_runnable",
    "load_python_entry",
    "sha256_file",
    "sha256_str",
    "trust_state",
]

__version__ = "0.1.0"
