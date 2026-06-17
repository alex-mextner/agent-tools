"""providers — the thin bridge that reuses the shared ``agenttools_providers`` CORE.

This is the "reuse providers verbatim" seam. research-cli does NOT re-derive a model
table, a failover order, or a key cascade: it imports the merged CORE (agent-tools#49)
and only supplies the data the CORE leaves to the tool — a default *research* board (a
different lens than review's) and the per-provider key names.

WHY a default board lives here, not in the CORE
    The CORE is data-structures + pure functions; the concrete board is a TOOL's choice.
    review-cli seeds the same shared manifest with a code-review board; research-cli seeds
    a research board (broad-reasoning lenses, not code-review lenses). Both resolve their
    seats against the same ``lib/contracts/models.yaml`` via the same ``resolve_role``.

NETWORK-FREE: like the CORE, nothing here calls a model. Resolving a seat to a concrete
``ModelEntry`` and picking a key NAME is pure; the live call is in :mod:`transport`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

# --- Import the shared CORE -------------------------------------------------------------
# research-cli is scaffolded inside the agent-tools umbrella, next to lib/. The CORE
# package is `lib/agenttools_providers`. Add `lib/` to sys.path so the import resolves
# from a source checkout WITHOUT an install step (the same pattern tests/ use). When
# research-cli is spun out to its own repo it depends on the published
# `agenttools-providers` distribution instead and this shim is dropped.
_LIB = Path(__file__).resolve().parents[3] / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_providers import (  # noqa: E402  (after sys.path injection)
    Board,
    BoardSeat,
    KeyCascade,
    ModelEntry,
    ProviderError,
    Registry,
    board_from_seats,
    load_registry,
    resolve_role,
)

# Re-export the CORE names a consumer of THIS module needs, so callers import one place.
__all__ = [
    "Board",
    "BoardSeat",
    "KeyCascade",
    "ModelEntry",
    "ProviderError",
    "Registry",
    "DEFAULT_RESEARCH_BOARD",
    "PROVIDER_KEY_NAMES",
    "default_manifest_path",
    "load_research_registry",
    "research_board",
    "resolve_seat",
    "key_cascade_for",
]


def default_manifest_path() -> Path:
    """Path to the ecosystem's shared model manifest (``lib/contracts/models.yaml``).

    Same manifest review-cli / task-cli read; research-cli does not fork it.
    """
    return _LIB / "contracts" / "models.yaml"


def load_research_registry(path: Optional[Path] = None) -> Registry:
    """Load the shared model registry from the manifest (default: the ecosystem manifest).

    Thin pass-through to the CORE's ``load_registry`` — kept here so callers depend on the
    research-cli surface, not directly on a CORE file path. Raises :class:`ProviderError`
    (from the CORE) on a missing/invalid manifest, with the CORE's actionable message.
    """
    return load_registry(path or default_manifest_path())


# --- The default RESEARCH board ---------------------------------------------------------
# Priority-ordered, strongest first (the CORE treats list order as priority). The seats
# are SYMBOLIC role names resolved against the manifest's `roles:` — so a manifest bump
# (a newer model behind `reasoning`) flows in without editing this list. Lenses are a
# research panel's lenses (a broad analyst, a skeptic, a fast wide-context pass), NOT
# review-cli's code-review lenses (correctness/security/tests) — that is the whole point
# of research-cli being a distinct tool.
DEFAULT_RESEARCH_BOARD: Tuple[Mapping[str, str], ...] = (
    {"model": "reasoning", "role": "analyst", "name": "Analyst"},
    {"model": "architect", "role": "skeptic", "name": "Skeptic"},
    {"model": "fast", "role": "scout", "name": "Scout"},
)

# Per-provider key NAMES (canonical first, then aliases) for the CORE's KeyCascade. The
# CORE resolves the VALUE (env beats .env files, name precedence beats file order); this
# map only says which NAMES a provider accepts. A provider missing here resolves to no
# key, which the transport treats as "not reachable" and the board skips.
PROVIDER_KEY_NAMES: Mapping[str, Tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "commandcode": ("COMMANDCODE_API_KEY", "CMD_API_KEY"),
    "zai": ("ZAI_API_KEY", "ZHIPU_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY",),
}


def research_board(
    seats: Optional[Sequence[Mapping[str, str]]] = None,
) -> Board:
    """Build the failover :class:`Board` for a research run (default: the research board).

    Pure delegation to the CORE's ``board_from_seats`` — list order is priority. A tool
    config can pass its own seats; otherwise the default research board is used.
    """
    return board_from_seats(seats if seats is not None else DEFAULT_RESEARCH_BOARD)


def resolve_seat(registry: Registry, seat: BoardSeat) -> ModelEntry:
    """Resolve a board seat's (possibly symbolic) model to a concrete :class:`ModelEntry`.

    The seat's ``model`` may be a concrete id OR a symbolic role/alias; ``resolve_role``
    (the CORE) honors both, an exact id winning first. Raises :class:`ProviderError` from
    the CORE on an unknown role — never a silent wrong pick.
    """
    return resolve_role(registry, seat.model)


def key_cascade_for(provider: str) -> Optional[KeyCascade]:
    """The :class:`KeyCascade` for ``provider``, or None if the provider has no known key.

    The cascade itself is the CORE's pure resolver; this only supplies the NAMES. The
    caller resolves the value (``cascade.resolve()``) and decides reachability.
    """
    names = PROVIDER_KEY_NAMES.get(provider)
    if not names:
        return None
    return KeyCascade(names=names)
