# agenttools-providers

The tool-agnostic **CORE** of the multi-model provider abstraction for the agent-tools
ecosystem — the asset ROADMAP §3.3 calls *"the biggest"*: board, failover, capability
tags, role resolution, key cascade, model-currency manifest. This package is the
distilled, **network-free CORE** of it: *data structures + pure functions*. It decides
**which** model / seat / key to use; the consuming tool owns **how** to reach it.

It is **stdlib-only at import**. The single optional dependency — PyYAML — is needed only
to parse a manifest **file** (`load_registry`) and is imported lazily, so a tool that
builds a `Registry` from in-memory data never pays for it.

Consumed by: `review-cli`, `task-cli` (its classifier's model pick), a future
`research-cli`.

## What this round extracts (the CORE)

| Piece | Type | What it does |
| --- | --- | --- |
| `Capability`, `KNOWN_CAPABILITIES` | value | the closed tag vocabulary (`vision`, `code`, `reasoning`, `tools`, `embeddings`, `audio`) — same enum as the manifest schema |
| `ModelEntry` / `make_entry` | data | one concrete model pin: id + provider + capability tags (+ context, notes) |
| `Registry` / `build_registry` | data | the in-memory registry + query helpers (`by_provider`, `entry`, `with_capability`) |
| `Registry.with_capability("vision")` | pure fn | **capability-tag filtering** — only genuinely vision-capable models (the #3681 image-review filter) |
| `resolve_role` | pure fn | **role -> model resolution that honors tags** — role `vision` resolves only to a vision-capable entry, else a loud error |
| `validate_registry` | pure fn | the cross-references the schema can't express (dangling targets, `vision` role on a text-only model, `<provider>:latest` mismatch, duplicate ids) |
| `Board` / `BoardSeat` / `board_from_seats` | data | a **priority-ordered failover board** of seats (model + role/lens + display) |
| `Board.pool` / `Board.split` / `failover_order` | pure fn | the **failover order**: top-N reachable seats + the reserve that backfills a failed one (availability predicate **injected**) |
| `KeyCascade` / `read_dotenv_value` | pure fn | the **key cascade**: env-name precedence first, then `.env` files (env + reader **injected**) |
| `load_registry` / `registry_from_mapping` | loader | parse `lib/contracts/models.yaml` (the model-currency manifest) into a `Registry` (lazy YAML) |

### Why these are the CORE

Each is something every tool would otherwise re-derive slightly differently:

- **Capability tags + the vision filter** are load-bearing and easy to get subtly wrong
  (Kimi-K2.7-Code is code-only — it must *not* be picked to verify an image; kimi-k2p6
  -turbo *is* vision-capable). One filter, one place.
- **Role resolution honoring tags** turns a symbolic ask (`"vision"`, `"architect"`)
  into a concrete model *and refuses* to hand back a model that lacks a role's implied
  capability — a misconfigured `vision: <text-only-model>` fails loudly at build/resolve
  time, never silently.
- **Failover ordering** is pure list math (priority slice + reserve). Keeping it pure —
  the availability check is injected, never performed here — makes it trivially testable
  and reusable, while the tool keeps ownership of the real probing/calling.
- **The key cascade** has a non-obvious invariant (key-**name** precedence beats file
  order, so the canonical name in a *later* file beats an *alias* in an *earlier* one).
  Re-implementing that per tool is how they drift; this is the one shared copy, made pure
  by injecting the environment and the file reader.

## Quick start

```python
from agenttools_providers import (
    make_entry, build_registry, resolve_role, Capability,
    BoardSeat, Board, failover_order, KeyCascade,
)

# 1. A capability-tagged registry (built from data — no YAML needed).
registry = build_registry(
    models=[
        make_entry("claude-opus-4-8", "anthropic", ["vision", "reasoning", "code"]),
        make_entry("kimi-k2.7-code",  "commandcode", ["code", "reasoning"]),  # NO vision
        make_entry("kimi-k2p6-turbo", "commandcode", ["vision", "code", "reasoning"]),
    ],
    roles={"reasoning": "claude-opus-4-8", "code": "kimi-k2.7-code", "vision": "kimi-k2p6-turbo"},
)

# 2. Capability-tag filtering (the image-review filter):
[m.id for m in registry.with_capability("vision")]
# -> ['claude-opus-4-8', 'kimi-k2p6-turbo']   (kimi-k2.7-code excluded)

# 3. Role -> model, honoring tags:
resolve_role(registry, "reasoning").id          # 'claude-opus-4-8'
resolve_role(registry, "vision").id             # 'kimi-k2p6-turbo' (must be vision-capable)
resolve_role(registry, "code", require_capability="vision")  # ProviderError — kimi-k2.7-code has no vision

# 4. A failover board (priority = list order, strongest first):
board = Board(seats=(
    BoardSeat("claude-opus-4-8", role="correctness", display="Opus"),
    BoardSeat("codex",          role="consistency", display="Codex"),
    BoardSeat("kimi-k2.7-code", role="performance",  display="Kimi"),
))
pool, reserve = board.split(2)                  # top-2 run; the rest backfill failures
# A seat unreachable at startup is SKIPPED and the next-priority one promoted:
def reachable(seat): return seat.model != "codex"
[s.display for s in board.pool(2, reachable)]   # ['Opus', 'Kimi']

# 5. The key cascade (env beats files; name precedence beats file order):
cascade = KeyCascade(names=("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"))
cascade.resolve(env={"ANTHROPIC_API_KEY": "sk-…"})   # 'sk-…'
```

Load the ecosystem's shared manifest (`lib/contracts/models.yaml` — the model-currency
board) directly:

```python
from agenttools_providers import load_registry, resolve_role
registry = load_registry("lib/contracts/models.yaml")
resolve_role(registry, "vision").id     # the current vision-capable pin
```

## Deferred (NOT in this round)

The **transport** half stays in the consuming tool — this CORE is deliberately network-
free. Deferred, by design:

- **Transports / live calls.** The network (`urllib`) and subprocess (`codex exec`,
  `claude -p`, `opencode run`) backends, request building, response parsing, sidecar
  logging, timeouts. This module never imports an HTTP client and never shells out.
- **`oc:` / `opencode:` provider routing.** Mapping a model string to a concrete backend
  function (`resolve_backend`) and routing `oc:<provider>/<model>` through opencode lives
  with the transports.
- **`api` | `cli` transport-mode selection** (`REVIEW_<NAME>_MODE`, `resolve_backend_mode`)
  and the claude api-vs-cli dispatch — a transport concern.
- **Live availability probing.** This module takes an availability **predicate** for the
  failover (so it stays pure); the actual "is the CLI on PATH / is the key set / did the
  endpoint answer" checks belong to the tool. Likewise mid-run failover *execution* (the
  loop that promotes a reserve when a seat fails *during* a run) — this CORE gives the
  ordering, the tool runs it.
- **The currency checker.** Polling provider `/models` and opening a bump PR
  (`lib/checker/model_freshness.py`) is a separate concern; this module only *reads* the
  manifest it maintains.

These can be layered on later (a thin transports module that imports this CORE and adds
the network), without touching the data + resolution logic here.

## Relationship to `lib/contracts/models.yaml`

`models.yaml` is the **data** (the model-currency manifest, validated by
`models.schema.json`); this package is the **reusable code** that loads, filters, and
resolves against it. `load_registry` parses that exact shape, and `validate_registry`
enforces the same cross-reference invariants the manifest's `--validate` does (notably:
the `vision` role/alias resolves only to a vision-capable entry). They are kept in
lockstep — the capability vocabulary here mirrors the schema's `capability` enum.

> Note: `lib/checker/model_freshness.py` carries its OWN `Manifest`/`ModelEntry`
> dataclasses today (it predates this module). Migrating the checker to import this CORE
> is a follow-up — out of scope for this extraction round, which does **not** modify the
> checker or review-cli.

## Public API

```python
from agenttools_providers import (
    # capability vocabulary
    Capability, KNOWN_CAPABILITIES,
    # registry + entries
    ModelEntry, make_entry, Registry, build_registry,
    validate_registry, registry_from_mapping, load_registry,
    # role resolution
    resolve_role,
    # failover board
    BoardSeat, Board, board_from_seats, failover_order,
    # key cascade
    KeyCascade, read_dotenv_value,
    # errors
    ProviderError,
)
```

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the
`agenttools-providers` distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-providers"]
# add the extra only if you call load_registry() on a YAML file:
# dependencies = ["agenttools-providers[yaml]"]
```

For local/dev installs from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib/agenttools_providers      # editable
# or ad-hoc with uv:
uv run --with /path/to/agent-tools/lib/agenttools_providers \
  python -c "from agenttools_providers import resolve_role"
```

## Tests

```sh
uv run --with pytest --with pyyaml python -m pytest tests/test_agenttools_providers.py -q
```

The suite is deterministic and isolated: registries/boards are built from in-memory data,
the key cascade resolves against an injected env mapping + reader (no `os.environ`, no
disk), and the one filesystem touch (the YAML manifest path) uses pytest's `tmp_path`. No
network, no sleeps, no global state. Coverage: capability-tag filtering, role resolution
honoring tags (incl. the `vision`-only-to-vision-capable guard at build *and* resolve
time), failover ordering (pool / reserve split / startup skip-and-promote / lens-travels
-with-seat), and the key cascade precedence (env-over-files, name-over-file-order).
```
