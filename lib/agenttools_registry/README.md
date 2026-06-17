# agenttools-registry

The shared **TRUST KERNEL** + trust-gated registry for the agent-tools ecosystem —
**stdlib only**. One importable home for the algorithm two CLIs re-implement today
(ROADMAP §3.5: *"registry + trust-kernel — the hook-trust + visual-module-registry trust
algorithm, one place"*).

## Why this exists — what was found

The ecosystem implements **the same trust algorithm twice, in two languages**:

| Consumer | File | Payload it trusts | Pins |
| --- | --- | --- | --- |
| **review-cli** | `reviewlib/features/visual/registry.py` (Python) | per-project visual modules discovered at `<project>/.review/visual-modules.json`, loaded via `importlib` | `entry_sha256` + `activates_on` |
| **tg-cli** | `features/hooks/runner.ts` + `types.ts` (TypeScript) | `agents-hooks/v1` hook descriptors dropped in `~/.agents/hooks/…`, run via `exec` | `cmd_sha256` + `invocation_sha256` (cmd+args+timeout) + `on_error` |

The shared-lib design doc (`docs/specs/2026-06-15-shared-lib-architecture.md`, §1 and §5)
spells it out: both are *"discover dropped-in descriptors → trust-by-default → opt-in TOFU
sha-pin guard → append-only `audit.jsonl` → inert-not-blocking on quarantine"*. That is
**one trust kernel implemented twice**. The other trust touch-point found — review-cli's
`_ensure_workspace_trusted` in `reviewlib/backends.py` (seeds Claude Code's
`~/.claude.json` workspace-trust flag) — is a **different** concern (pre-accepting an
interactive TUI gate, not a provenance/allowlist decision over dropped-in code) and is
**not** folded in here.

This module generalizes the shared algorithm so neither consumer's payload shape leaks in.
It does **not** modify either consumer — they *could* adopt it (see *Migration seam*).

## The algorithm

1. **Trust-by-default.** With the guard **off** (the common case — your own repo, your own
   dropped-in modules) every entry whose `cmd` exists is `trusted-default` and runs with
   no pin and no ceremony. Reviewing/hooking your OWN code must not nag.
2. **Opt-in guard.** A truthy `guard` (the consumers' `REVIEW_UNTRUSTED_MODULES=1` /
   `AGENTS_HOOKS_TRUST=1`) re-engages a TOFU (trust-on-first-use) quarantine for the rare
   untrusted-input case (an external PR, a cloned stranger's repo):
   - never-seen entry → `quarantined-new` (inert; a loud banner tells the user how to
     trust it);
   - `cmd` bytes or invocation digest changed since the pin → `quarantined-changed`;
   - matching pin → `trusted`.
3. **`auto` escape hatch.** `auto=True` (`…=auto` in the consumers) bypasses the pins under
   the guard — the batch/agent escape hatch. No effect when the guard is off.
4. **Missing cmd** → `untrusted-missing-cmd` regardless of the guard (nothing to run).
5. **Quarantine is inert.** A quarantined / missing entry is treated as **absent** — it
   never runs and never blocks.
6. **Audit.** Every decision is one append-only JSON line. Best-effort: an audit failure
   never breaks the caller.

`is_runnable(state)` is true iff the state is `trusted-default` / `trusted` / `auto`.

The `TrustState` string values (`trusted-default`, `trusted`, `auto`, `quarantined-new`,
`quarantined-changed`, `untrusted-missing-cmd`) and the NUL-separated `invocation_digest`
layout match **tg-cli's TS `TrustState` union and `invocationDigest` byte-for-byte**, so a
shared `audit.jsonl` reads the same across the Python and TS hosts.

## Usage

```python
from agenttools_registry import Entry, Registry, TrustState

# Trust-by-default (your own repo): every entry whose cmd exists is runnable.
reg = Registry(store_path=store, audit_path=audit, guard=False)
entries = [Entry(name="my-mod", cmd=path_to_entry_py, activates_on_tags)]
result = reg.gate(entries)

for ge in result.runnable:        # trusted-default / trusted / auto
    run(ge.entry)                 # YOU own how to run it (importlib, exec, …)
for ge in result.quarantined:     # inert — absent, never a block
    print("quarantined:", ge.entry.name, "—", ge.decision.reason)
```

Under the opt-in guard, pin an entry once (the `trust` verb):

```python
reg = Registry(store_path=store, guard=True)
reg.trust(entry)   # writes {cmd_sha256, invocation_sha256} pin to store (0600)
```

For a contributed Python entry there's a loader that mirrors review's `_load_entry_object`
(looks for a top-level `MODULE`, then a `module()`/`get_module()` factory, then a `Module`
class) — **gate first, then load**:

```python
from agenttools_registry import load_python_entry
for ge in result.runnable:
    obj = load_python_entry(ge.entry.cmd)   # None if the file is broken
```

## The `Entry` shape

An `Entry` is the payload-agnostic union of review's `ModuleSpec` and tg's
`HookDescriptor`:

| Field | Meaning |
| --- | --- |
| `name` | store key / audit correlation (review's module name, tg's `id.point`) |
| `cmd` | absolute path to the executable / loadable file whose **bytes** are pinned |
| `args` | argv tokens folded into the invocation digest (tg) |
| `timeout_ms` | per-run timeout, folded into the digest (a forced timeout on a fail-open gate becomes an allow) |
| `extra_digest` | fields that must re-quarantine on change without touching the cmd bytes — review pins `activates_on` here |
| `meta` | opaque consumer payload (review's runtime/description, tg's `on_error`); the kernel never interprets it |

## Trust store & audit

- **Trust store** — a JSON file of `{name -> {cmd_sha256, invocation_sha256, trusted_at,
  meta}}`, written **`0600`** on every save (it gates arbitrary code execution). A missing
  / garbage file loads as an empty store (never an error). Mirrors review's
  `modules-trust.json` and tg's `TrustStore`.
- **Audit** — `audit_decision(path, decision, outcome=…)` appends one JSON line per
  decision. Best-effort; never raises. Mirrors review's `_audit` and tg's `AuditLine`.

## Safety posture

- **Trust-by-default is the default.** The guard is opt-in; you never get nagged on your
  own code.
- **Quarantine never blocks.** A quarantined or missing entry is absent, never a hard
  failure — a guard misconfiguration degrades to "doesn't run", not "everything breaks".
- **Pins are `0600`.** The trust store gates code execution; it's chmod-ed on every write.
- **Auditing never breaks the caller.** Every audit write swallows its own `OSError`.
- **`importlib` is lazy.** The trust *decision* imports nothing heavy; `importlib` is
  imported only inside `load_python_entry`, and only when you actually load a Python entry.

## Public API

| Symbol | Purpose |
| --- | --- |
| `Entry` | a discovered registry entry (payload-agnostic) |
| `TrustState` | the six trust states (string enum, tg-compatible values) |
| `TrustDecision` | a verdict: state + reason + freshly computed digests + `runnable` |
| `trust_state(entry, store, *, guard, auto=False) -> TrustDecision` | the kernel decision |
| `is_runnable(state)` / `is_quarantined(state)` | state classifiers |
| `TrustStore` | `{name -> TrustPin}` JSON store (`load` / `pin` / `save`, 0600) |
| `TrustPin` | a pinned entry (`cmd_sha256`, `invocation_sha256`, `trusted_at`, `meta`) |
| `Registry` | facade: `gate(entries)` (split runnable/quarantined + audit), `trust(entry)` |
| `GateResult` / `GatedEntry` | the `gate()` result split |
| `audit_decision(path, decision, *, outcome, …)` | append one audit row (best-effort) |
| `sha256_file` / `sha256_str` / `invocation_digest` / `entry_invocation_digest` | digest helpers |
| `load_python_entry(cmd, …)` | optional importlib loader for a Python entry |

## Installing / importing as a consumer

The package lives under `lib/agenttools_registry/` in the umbrella repo and builds as the
`agenttools-registry` distribution (import name `agenttools_registry`):

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-registry"]
```

For local/dev use without an install, add `lib/` to `sys.path` (the tests do this) or:

```sh
uv run --with /path/to/agent-tools/lib/agenttools_registry python -c "import agenttools_registry"
```

### Migration seam for existing consumers

This package ships **standalone** — wiring the consumers is a deliberate follow-up and is
**not** done here:

- **review-cli** — `reviewlib/features/visual/registry.py` could express each `ModuleSpec`
  as an `Entry` (`cmd=entry_path`, `extra_digest=activates_on`) and replace
  `_trust_state_for` + `_load_trust`/`_write_trust` + `_audit` with this kernel, keeping
  its `VisualModule` Protocol check and `REVIEW_UNTRUSTED_MODULES` env name as the `guard`.
- **tg-cli** — `features/hooks/runner.ts` is TypeScript; this is the **Python** kernel.
  A TS twin over the same algorithm (or a thin TS port that reads the same store/audit
  shapes) is the cross-language story; the string values and digest layout here are
  already tg-compatible so the two stay interoperable.
```
