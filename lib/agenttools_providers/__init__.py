"""agenttools_providers — the tool-agnostic CORE of the multi-model provider abstraction.

The "providers" asset, distilled to its reusable, network-free CORE so review-cli,
task-cli's classifier, and a future research-cli stop each re-deriving model selection
from their own ad-hoc tables. This package is *data structures + pure functions*: it
decides WHICH model/seat/key to use; the consuming tool owns HOW to reach it.

What it gives you
-----------------
* A **provider/model registry with capability tags** — :class:`Registry` of
  :class:`ModelEntry`, each carrying :class:`Capability` tags (``vision`` / ``code`` /
  ``reasoning`` / ``tools`` / ``embeddings`` / ``audio``), with capability-tag filtering
  (``registry.with_capability("vision")`` — the load-bearing image-review filter).
* **Role -> model resolution that honors tags** — :func:`resolve_role`; a role whose
  name is a capability (``vision``) resolves only to a model carrying it, else a loud
  :class:`ProviderError`.
* **A failover order** — :class:`Board` of :class:`BoardSeat`\\ s, priority-ordered, with
  :func:`failover_order` / :meth:`Board.pool` / :meth:`Board.split` giving the
  deterministic top-N-reachable pool + reserve (availability predicate is injected).
* **A key-cascade resolver** — :class:`KeyCascade`: env-name precedence first, then
  ``.env`` files; environment + reader injected, so resolution is pure and testable.

Deferred (stays in the consuming tool): the transports — network/subprocess backends,
``oc:`` / ``opencode:`` provider routing, ``api``|``cli`` transport-mode selection, and
every live model call. See ``lib/agenttools_providers/README.md`` for the full
extracted-vs-deferred split.

Quick start
-----------
    from agenttools_providers import (
        make_entry, build_registry, resolve_role, Capability,
        BoardSeat, Board, failover_order, KeyCascade,
    )

    registry = build_registry(
        models=[
            make_entry("claude-opus-4-8", "anthropic", ["vision", "reasoning", "code"]),
            make_entry("kimi-k2.7-code", "commandcode", ["code", "reasoning"]),
        ],
        roles={"reasoning": "claude-opus-4-8", "code": "kimi-k2.7-code"},
    )
    registry.with_capability("vision")          # only vision-capable entries
    resolve_role(registry, "reasoning").id       # 'claude-opus-4-8'

    board = Board(seats=(
        BoardSeat("claude-opus-4-8", role="correctness", display="Opus"),
        BoardSeat("kimi-k2.7-code", role="performance", display="Kimi"),
    ))
    pool, reserve = board.split(1)               # top-1 + the rest as reserve

    cascade = KeyCascade(names=("ANTHROPIC_API_KEY",))
    cascade.resolve(env={"ANTHROPIC_API_KEY": "sk-…"})

Or load the ecosystem's shared manifest (``lib/contracts/models.yaml``) directly:

    from agenttools_providers import load_registry
    registry = load_registry("lib/contracts/models.yaml")
"""

from __future__ import annotations

from .core import (
    KNOWN_CAPABILITIES,
    Board,
    BoardSeat,
    Capability,
    KeyCascade,
    ModelEntry,
    ProviderError,
    Registry,
    board_from_seats,
    build_registry,
    failover_order,
    load_registry,
    make_entry,
    read_dotenv_value,
    registry_from_mapping,
    resolve_role,
    validate_registry,
)

__all__ = [
    "KNOWN_CAPABILITIES",
    "Board",
    "BoardSeat",
    "Capability",
    "KeyCascade",
    "ModelEntry",
    "ProviderError",
    "Registry",
    "board_from_seats",
    "build_registry",
    "failover_order",
    "load_registry",
    "make_entry",
    "read_dotenv_value",
    "registry_from_mapping",
    "resolve_role",
    "validate_registry",
]

__version__ = "0.1.0"
