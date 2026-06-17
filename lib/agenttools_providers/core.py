"""Core of the tool-agnostic provider/model abstraction.

The public surface (``Capability``, ``ModelEntry``, ``Registry``, ``Board``,
``BoardSeat``, ``KeyCascade``, ``resolve_role``, ``failover_order``,
``load_registry``, ``ProviderError``) is re-exported from the package ``__init__``;
import from there.

What this module IS (the extracted CORE)
----------------------------------------
A *data + pure-function* layer that several tools (review-cli, task-cli's classifier,
a future research-cli) can share instead of each re-deriving model selection from its
own ad-hoc tables:

* **A provider/model registry with capability tags.** :class:`ModelEntry` is one
  concrete, provider-resolvable model id plus its provider and a frozen set of
  :class:`Capability` tags (``vision`` / ``code`` / ``reasoning`` / ``tools`` /
  ``embeddings`` / ``audio``). :class:`Registry` is the in-memory collection with the
  query helpers (by provider, by capability, by id).
* **Capability-tag filtering.** ``registry.with_capability("vision")`` returns only
  the entries that genuinely carry the tag — the load-bearing image-review filter
  (review-cli #3681): a model without real image input must never be picked to
  "verify" an image.
* **Role -> model resolution that HONORS tags.** A *role* is a symbolic lens a tool
  asks for ("architect", "reasoning", "vision", ...). :func:`resolve_role` maps it to
  a concrete :class:`ModelEntry` via the registry's ``roles`` map, and a role whose
  name is itself a capability (e.g. ``vision``) is *required* to resolve to an entry
  that carries that capability — a misconfigured ``vision: <text-only-model>`` is a
  loud :class:`ProviderError`, not a silent wrong pick.
* **A failover order.** A :class:`Board` is a *priority-ordered* list of
  :class:`BoardSeat`\\ s (strongest first). :func:`failover_order` /
  :meth:`Board.pool` give the deterministic "run the top-N reachable seats, keep the
  rest as the reserve that backfills a failed seat" ordering — the startup + mid-run
  failover review-cli's board does, distilled to its data shape (an availability
  predicate is *injected*, so this stays pure: no probing, no network).
* **A key-cascade resolver.** :class:`KeyCascade` resolves a provider API key by
  *name precedence first*: an environment variable beats every file, and among the
  files the first accepted name wins regardless of which file it sits in. The
  environment and the per-file reader are both *injected*, so resolution is a pure
  function of its inputs and tests touch neither ``os.environ`` nor the disk.

What this module is NOT (deferred — see README)
-----------------------------------------------
The *transport* half stays in the consuming tool: the network/subprocess backends, the
``oc:`` / ``opencode:`` provider routing, the ``api``|``cli`` transport-mode selection,
and every live model call. This CORE decides *which* model/seat/key to use; the tool
owns *how* to reach it. Nothing here imports a network client or shells out.

Stdlib-only at import. The one optional dependency — PyYAML, needed only to parse a
manifest *file* via :func:`load_registry` — is imported lazily inside that function, so
a tool that builds a :class:`Registry` from in-memory data never pays for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple


class ProviderError(ValueError):
    """A registry/manifest/role configuration is malformed or self-contradictory.

    Raised loudly rather than degrading to a wrong-but-plausible pick — e.g. a role
    named after a capability (``vision``) that resolves to a model lacking that
    capability, or a role/alias pointing at an id absent from the registry.
    """


# The closed capability vocabulary. Kept byte-identical to the manifest schema's
# `capability` enum (lib/contracts/models.schema.json) so a Registry built from that
# manifest and one built in code speak the same tags. `vision` is load-bearing: the
# image-review path filters the registry to vision-capable entries (review-cli #3681),
# so a model WITHOUT real image input must never carry it.
KNOWN_CAPABILITIES: FrozenSet[str] = frozenset(
    {"vision", "code", "reasoning", "tools", "embeddings", "audio"}
)


class Capability(str):
    """A capability tag, normalised to lower-case.

    A thin ``str`` subclass so a capability is still a plain string everywhere (it
    compares, hashes, and serialises like ``"vision"``) while construction enforces
    the closed vocabulary — an unknown tag is a :class:`ProviderError`, never a typo
    that silently never matches. The well-known tags are exposed as class attributes
    (``Capability.VISION`` …) for call sites that prefer a symbol over a literal.
    """

    __slots__ = ()

    VISION: "Capability"
    CODE: "Capability"
    REASONING: "Capability"
    TOOLS: "Capability"
    EMBEDDINGS: "Capability"
    AUDIO: "Capability"

    def __new__(cls, value: str) -> "Capability":
        norm = str(value).strip().lower()
        if norm not in KNOWN_CAPABILITIES:
            raise ProviderError(
                f"unknown capability {value!r}; known capabilities are "
                f"{', '.join(sorted(KNOWN_CAPABILITIES))}"
            )
        return super().__new__(cls, norm)


Capability.VISION = Capability("vision")
Capability.CODE = Capability("code")
Capability.REASONING = Capability("reasoning")
Capability.TOOLS = Capability("tools")
Capability.EMBEDDINGS = Capability("embeddings")
Capability.AUDIO = Capability("audio")


# The ONE capability a role/alias name can imply. Vision is load-bearing — the image-review
# path (#3681) filters the registry to vision-capable entries — so a `vision` / `*:vision`
# pointer must resolve to a vision-capable model. The other capabilities (code/reasoning/…)
# gate no such filter, so a `:code` alias carries no implicit demand. Lifted to a constant
# so the hardcoding is visible at the call sites and a future second name-constrained
# capability is a one-line change here, not a hunt through the validators.
NAME_CONSTRAINED_CAPABILITY = "vision"


def _required_capability_of(name: str) -> Optional[str]:
    """The capability a role/alias NAME requires of its target, or ``None``.

    A bare ``"vision"`` requires vision, and so does a qualified ``"<provider>:vision"``
    alias (``"commandcode:vision"``). Everything else — a plain role like ``"architect"``,
    or a ``<provider>:latest`` pointer — requires nothing. This mirrors the manifest
    checker's ``key == "vision" or key.endswith(":vision")`` test (#3681): the SAME single
    name-implied capability and the SAME canonical lower-case form models.yaml is written
    in, so a Registry built in code and one built from a manifest enforce the identical
    cross-reference. Core normalises the name first where the checker treats keys raw — a
    deliberately STRICTER superset: core also constrains an upper-cased ``"VISION"`` the
    checker would miss, never the reverse, so core can only reject MORE, never silently
    accept a non-vision ``:vision`` the checker would catch.
    """
    norm = name.strip().lower()
    if norm == NAME_CONSTRAINED_CAPABILITY or norm.endswith(f":{NAME_CONSTRAINED_CAPABILITY}"):
        return NAME_CONSTRAINED_CAPABILITY
    return None


@dataclass(frozen=True)
class ModelEntry:
    """One concrete model pin: a provider-resolvable id, its provider, and its tags.

    ``id`` is a concrete model string (byte-exact against the provider catalog), never
    a symbolic alias — aliases live in the registry's ``roles`` / ``aliases`` maps.
    ``capabilities`` is a frozenset of :class:`Capability`, so ``"vision" in
    entry.capabilities`` is the membership test. ``context`` is the advertised input
    window in tokens (optional); ``notes`` is free-form provenance.
    """

    id: str
    provider: str
    capabilities: FrozenSet[Capability]
    context: Optional[int] = None
    notes: str = ""

    def has(self, capability: str) -> bool:
        """True if this model carries ``capability`` (case-insensitive)."""
        return Capability(capability) in self.capabilities


def _coerce_capabilities(caps: Iterable[str]) -> FrozenSet[Capability]:
    return frozenset(Capability(c) for c in caps)


def make_entry(
    id: str,  # noqa: A002 - 'id' mirrors the manifest field name on purpose
    provider: str,
    capabilities: Iterable[str],
    *,
    context: Optional[int] = None,
    notes: str = "",
) -> ModelEntry:
    """Build a :class:`ModelEntry`, validating + normalising its capability tags.

    A convenience over the dataclass constructor so callers pass plain strings for the
    tags (``["vision", "code"]``) and get a validated frozenset of :class:`Capability`
    — an unknown tag raises :class:`ProviderError` here, at construction, not later.
    """
    if not str(id).strip():
        raise ProviderError("model entry needs a non-empty 'id'")
    if not str(provider).strip():
        raise ProviderError(f"model {id!r} needs a non-empty 'provider'")
    coerced = _coerce_capabilities(capabilities)
    if not coerced:
        raise ProviderError(f"model {id!r} needs at least one capability")
    return ModelEntry(
        id=str(id).strip(),
        provider=str(provider).strip(),
        capabilities=coerced,
        context=context,
        notes=str(notes),
    )


@dataclass(frozen=True)
class Registry:
    """The in-memory provider/model registry: the entries plus the symbolic maps.

    ``models`` is the ordered tuple of :class:`ModelEntry` (within a provider,
    strongest/preferred first — the order callers may treat as priority). ``roles``
    maps a symbolic lens ("architect", "vision", ...) to a concrete model id; ``aliases``
    maps convenience names and the ``<provider>:latest`` pointers the same way. Build
    one from data with :func:`build_registry` / :func:`load_registry`.
    """

    models: Tuple[ModelEntry, ...]
    roles: Mapping[str, str] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)

    def entry(self, model_id: str) -> Optional[ModelEntry]:
        """The entry with this exact id, or None."""
        return next((m for m in self.models if m.id == model_id), None)

    def providers(self) -> list:
        """The distinct providers, in first-seen (priority) order."""
        seen: list = []
        for m in self.models:
            if m.provider not in seen:
                seen.append(m.provider)
        return seen

    def by_provider(self, provider: str) -> list:
        """Every entry for ``provider``, in registry (priority) order."""
        return [m for m in self.models if m.provider == provider]

    def with_capability(self, capability: str) -> list:
        """Every entry carrying ``capability``, in registry (priority) order.

        The image-review filter (review-cli #3681) is exactly
        ``registry.with_capability("vision")`` — only genuinely vision-capable models.
        An unknown capability name raises :class:`ProviderError` (a typo must fail loud,
        not return an empty list that looks like "no vision models").
        """
        cap = Capability(capability)
        return [m for m in self.models if cap in m.capabilities]


def build_registry(
    models: Iterable[ModelEntry],
    roles: Optional[Mapping[str, str]] = None,
    aliases: Optional[Mapping[str, str]] = None,
    *,
    validate: bool = True,
) -> Registry:
    """Assemble a :class:`Registry` from entries + the symbolic maps.

    With ``validate=True`` (default) the cross-references a per-entry constructor cannot
    see are checked up front via :func:`validate_registry`, so a bad ``roles`` /
    ``aliases`` target — or a capability-named role pointing at an entry lacking that
    capability — is a :class:`ProviderError` at build time, not a surprise at resolve
    time. Pass ``validate=False`` only when you have already validated.
    """
    reg = Registry(
        models=tuple(models),
        roles=dict(roles or {}),
        aliases=dict(aliases or {}),
    )
    if validate:
        problems = validate_registry(reg)
        if problems:
            raise ProviderError("invalid registry:\n  - " + "\n  - ".join(problems))
    return reg


def validate_registry(registry: Registry) -> list:
    """Return a list of human-readable problems ([] == valid).

    Enforces the cross-references a single :class:`ModelEntry` cannot see — the same
    invariants the manifest's ``--validate`` checks:

    * every ``roles`` / ``aliases`` target is a concrete id present in ``models``;
    * a role/alias whose NAME is a capability (e.g. ``vision``) resolves only to an
      entry that carries that capability (the #3681 cross-reference);
    * ``<provider>:latest`` points at an entry of THAT provider;
    * no duplicate model ids.
    """
    problems: list = []
    ids = {m.id for m in registry.models}

    seen_ids: set = set()
    for m in registry.models:
        if m.id in seen_ids:
            problems.append(f"duplicate model id {m.id!r}")
        seen_ids.add(m.id)

    def _check_target(kind: str, name: str, target: str) -> Optional[ModelEntry]:
        if target not in ids:
            problems.append(
                f"{kind} {name!r} -> {target!r} which is not a model id in the registry"
            )
            return None
        return registry.entry(target)

    def _check_capability(kind: str, name: str, entry: ModelEntry, target: str) -> None:
        # A role/alias whose NAME is `vision` or ends in `:vision` (`commandcode:vision`)
        # must resolve to a vision-capable entry — the image-review filter (#3681) — so a
        # `*:vision` pointer at a text-only model is rejected, not silently honoured. This
        # mirrors the manifest checker's `_check_pointer` rule; core additionally lower-cases
        # the name (see `_required_capability_of`), so it rejects a superset, never less.
        cap = _required_capability_of(name)
        if cap is not None and Capability(cap) not in entry.capabilities:
            problems.append(
                f"{kind} {name!r} resolves to {target!r}, which is not {cap}-capable "
                f"(capabilities: {', '.join(sorted(entry.capabilities)) or 'none'})"
            )

    def _check_no_id_collision(kind: str, name: str) -> None:
        # A role/alias key that is ALSO a concrete model id is dead: `resolve_role` resolves
        # an exact id before consulting roles/aliases, so the pointer never fires. Beyond the
        # general dead-pointer bug, this also lets a `*:vision` alias colliding with a
        # non-vision id silently return that model and bypass the #3681 guard. Reject the
        # collision so a validated registry can never hide one.
        if name in ids:
            problems.append(
                f"{kind} {name!r} collides with a concrete model id of the same name, which "
                f"shadows it at resolve time (a model id always wins over a role/alias)"
            )

    for role, target in registry.roles.items():
        _check_no_id_collision("role", role)
        entry = _check_target("role", role, target)
        if entry is not None:
            _check_capability("role", role, entry, target)

    for alias, target in registry.aliases.items():
        _check_no_id_collision("alias", alias)
        entry = _check_target("alias", alias, target)
        if entry is None:
            continue
        _check_capability("alias", alias, entry, target)
        # `<provider>:latest` must point at an entry of that same provider.
        if alias.endswith(":latest"):
            want = alias[: -len(":latest")]
            if entry.provider != want:
                problems.append(
                    f"alias {alias!r} -> {target!r} whose provider is "
                    f"{entry.provider!r}, not {want!r}"
                )
    return problems


def resolve_role(
    registry: Registry,
    role: str,
    *,
    require_capability: Optional[str] = None,
) -> ModelEntry:
    """Resolve a symbolic ``role`` (then ``aliases``) to a concrete :class:`ModelEntry`.

    Lookup order: an exact model id wins first (so a caller may pass a concrete id where
    a role is expected), then ``roles``, then ``aliases``. A SYMBOLIC name whose own text
    is ``vision`` or ends in ``:vision`` (e.g. ``commandcode:vision``) is required to
    resolve to a vision-capable entry — the same guard :func:`validate_registry` applies,
    enforced again here so a registry built with ``validate=False`` still cannot hand back
    a text-only model for a vision pointer. The name-implied guard is skipped for a direct
    concrete-id hit (a model literally named ``foo:vision`` is just that model, returned
    verbatim). Pass ``require_capability`` to demand a capability the name does not imply.

    Raises :class:`ProviderError` when the role/alias is unknown, points at a missing id,
    or resolves to an entry lacking a required capability — never a silent wrong pick.
    """
    direct = registry.entry(role)
    if direct is not None:
        # An exact concrete id was passed where a role is expected — honour it verbatim.
        # We do NOT read a capability out of the id's own text (a model literally named
        # `foo:vision` is just that model), so a direct hit skips the name-implied guard.
        target_id: str = role
        entry: ModelEntry = direct
        symbolic = False
    else:
        target_id = registry.roles.get(role) or registry.aliases.get(role) or ""
        if not target_id:
            known = sorted({*registry.roles, *registry.aliases})
            raise ProviderError(
                f"unknown role/alias {role!r}; known: {', '.join(known) or 'none'}"
            )
        resolved = registry.entry(target_id)
        if resolved is None:
            raise ProviderError(
                f"role/alias {role!r} -> {target_id!r}, which is not a model in the registry"
            )
        entry = resolved
        symbolic = True

    required: list = []  # capability names (str) to enforce on the resolved entry
    implied = _required_capability_of(role) if symbolic else None
    if implied is not None:
        required.append(implied)
    if require_capability is not None:
        required.append(str(Capability(require_capability)))
    for cap in required:
        if Capability(cap) not in entry.capabilities:
            raise ProviderError(
                f"role {role!r} resolves to {target_id!r}, which is not {cap}-capable "
                f"(capabilities: {', '.join(sorted(entry.capabilities)) or 'none'})"
            )
    return entry


# --- Failover board -------------------------------------------------------------------
# A board is a PRIORITY-ORDERED list of seats (strongest first). The order IS the
# priority; it drives the failover pool — a plain run takes the top-N reachable seats,
# the rest are the reserve that backfills a seat which is unreachable at startup or fails
# mid-run. Availability is INJECTED (a predicate), so this layer stays pure: it never
# probes a key or a binary, it just orders. The consuming tool supplies "is this
# reachable?" and owns the actual calls.


@dataclass(frozen=True)
class BoardSeat:
    """One seat on a failover board: a model id + a role/lens + a display name.

    ``model`` is the concrete model id (or a role the tool resolves via the registry).
    ``role`` is the lens this seat reviews with — it travels WITH the seat, so a promoted
    reserve brings its own lens. ``display`` is a short human label for listings/results.
    """

    model: str
    role: str = ""
    display: str = ""


def _always_available(_seat: BoardSeat) -> bool:
    return True


@dataclass(frozen=True)
class Board:
    """A priority-ordered failover board: the ordered seats + the pool/reserve split.

    The seats are expected strongest-first; that order is the priority. :meth:`pool` and
    :meth:`split` apply an injected availability predicate and a target pool size to
    produce the deterministic startup-failover slice (the top-N reachable seats) and its
    reserve (the rest, in priority order). Nothing here runs a model — see the module
    docstring for the deferred transport half.
    """

    seats: Tuple[BoardSeat, ...]

    @staticmethod
    def _effective_size(seat_count: int, pool: int) -> int:
        """How many seats a ``pool`` request selects from ``seat_count`` available seats:
        ``pool <= 0`` means ALL, a ``pool`` past the count is clamped, else exactly
        ``pool``. Single source of truth for :meth:`pool` and :meth:`split`."""
        if pool <= 0 or pool >= seat_count:
            return seat_count
        return pool

    def available(
        self, predicate: Callable[[BoardSeat], bool] = _always_available
    ) -> list:
        """The reachable seats, in priority order (the order the failover walks)."""
        return [s for s in self.seats if predicate(s)]

    def pool(
        self,
        size: int,
        predicate: Callable[[BoardSeat], bool] = _always_available,
    ) -> list:
        """The top ``size`` AVAILABLE seats by priority (startup-failover slice).

        A higher-priority seat the ``predicate`` rejects is SKIPPED and the next-priority
        one pulled up, so the run still starts with ``size`` working seats when enough
        reachable seats exist. ``size <= 0`` means "all available"; a ``size`` past the
        available count is clamped. Deterministic; returns a new list.
        """
        seats = self.available(predicate)
        return list(seats[: self._effective_size(len(seats), size)])

    def split(
        self,
        size: int,
        predicate: Callable[[BoardSeat], bool] = _always_available,
    ) -> Tuple[list, list]:
        """Split the AVAILABLE seats into ``(pool, reserve)`` by priority, for failover.

        ``pool`` = the top-N available seats (same slice :meth:`pool` returns); ``reserve``
        = the remaining available seats, in priority order, which backfill a pool seat
        that fails mid-run. Unreachable seats are in NEITHER list. The two are disjoint
        and together hold every available seat, in priority order.
        """
        seats = self.available(predicate)
        n = self._effective_size(len(seats), size)
        return list(seats[:n]), list(seats[n:])


def failover_order(
    board: Board,
    predicate: Callable[[BoardSeat], bool] = _always_available,
) -> list:
    """The full priority-ordered list of AVAILABLE seats — the order failover walks.

    Equivalent to ``board.available(predicate)``; the named free function exists because
    "failover order" is the concept callers reach for, and so the pool/reserve split
    (:meth:`Board.split`) and the flat order share one obvious entry point.
    """
    return board.available(predicate)


# --- Key cascade ----------------------------------------------------------------------
# Resolve a provider API key by NAME PRECEDENCE FIRST: an environment variable beats
# every file, and among the files the first accepted name wins regardless of which file
# it lives in. Both the environment mapping and the per-file reader are INJECTED, so
# resolution is a pure function of its inputs — tests touch neither os.environ nor the
# disk, and a tool wires in its real env + .env reader. (This is review-cli's
# `_resolve_key` precedence, distilled and made injectable.)


def read_dotenv_value(path: Path, var: str) -> Optional[str]:
    """Read ``VAR=value`` from a flat ``.env`` file (surrounding quotes stripped).

    Returns None on a miss or an unreadable file — a missing/locked env file is a skip,
    never an error. A small stdlib reader, good enough for the ``KEY=value`` files the
    ecosystem CLIs already write; not a full dotenv parser (no interpolation, no export).
    """
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


@dataclass(frozen=True)
class KeyCascade:
    """A pure key-cascade resolver: env-name precedence first, then files.

    Construct with the accepted key NAMES (canonical first, then aliases) and the ordered
    ``.env`` files to fall back to. :meth:`resolve` then applies the precedence:

    1. the environment — each name in order; the first set, non-empty value wins;
    2. the files — each NAME across ALL files before moving to the next name, so the
       canonical/primary name in a *later* file still beats an *alias* in an earlier one
       (key-name-first, deterministic, independent of file ordering).

    The environment is injected (``env=`` mapping) and so is the per-file reader
    (``reader=``), so resolution is a pure function of its inputs — no ``os.environ``, no
    disk, in tests. The defaults read the real process environment and flat ``.env``
    files, for production use.
    """

    names: Tuple[str, ...]
    files: Tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not self.names:
            raise ProviderError("KeyCascade needs at least one key name")

    def resolve(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
        reader: Optional[Callable[[Path, str], Optional[str]]] = None,
    ) -> Optional[str]:
        """Resolve the key, or None if no name resolves anywhere.

        ``env`` defaults to the real process environment; ``reader`` defaults to
        :func:`read_dotenv_value`. Both are injectable so the cascade is testable without
        touching global state.
        """
        environ = env if env is not None else _os_environ()
        read = reader if reader is not None else read_dotenv_value

        # 1. Environment wins, in declared name order (primary name first).
        for name in self.names:
            value = (environ.get(name, "") or "").strip()
            if value:
                return value
        # 2. Files: name-priority-first — each name across ALL files before the next name.
        for name in self.names:
            for path in self.files:
                from_file = read(path, name)
                if from_file:
                    return from_file
        return None


def _os_environ() -> Mapping[str, str]:
    """The real process environment, imported lazily so the module's import stays clean
    and the default is only materialised when a caller actually resolves against it."""
    import os

    return os.environ


# --- Manifest loading (the one place YAML is touched) ---------------------------------
# load_registry parses lib/contracts/models.yaml (or any file of that shape) into a
# Registry. YAML is imported LAZILY here so the module stays stdlib-only at import: a
# tool that builds a Registry from in-memory data (build_registry) never pays for PyYAML.


def load_registry(path: Path, *, validate: bool = True) -> Registry:
    """Parse a ``models.yaml``-shaped manifest into a :class:`Registry`.

    The manifest is the ecosystem's model board (``lib/contracts/models.yaml``): a
    ``models:`` list of ``{id, provider, capabilities[, context, notes]}`` plus optional
    ``roles:`` / ``aliases:`` maps. With ``validate=True`` (default) the cross-references
    are checked and a violation raises :class:`ProviderError`.

    PyYAML is imported lazily; without it a clear, actionable :class:`ProviderError` is
    raised rather than a raw ImportError. A malformed manifest is a :class:`ProviderError`,
    never a silently-empty registry.
    """
    try:
        import yaml  # lazy: keeps this module stdlib-only at IMPORT time
    except ImportError as exc:
        raise ProviderError(
            "PyYAML is required to read a manifest file but is not installed. "
            "Install it (`python3 -m pip install --user pyyaml`), or build the registry "
            "from data with build_registry() instead."
        ) from exc

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProviderError(f"cannot read manifest {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProviderError(f"invalid YAML in {path}: {exc}") from exc
    return registry_from_mapping(raw, validate=validate)


def registry_from_mapping(raw: object, *, validate: bool = True) -> Registry:
    """Build a :class:`Registry` from an already-parsed manifest mapping.

    Split out from :func:`load_registry` so a tool that already holds the parsed dict
    (or wants to test the structuring without a file) can reuse the exact same shaping
    and validation. Raises :class:`ProviderError` on a non-mapping / missing-required
    shape — a silently-empty registry would make every model "look new"/absent.
    """
    if not isinstance(raw, Mapping):
        raise ProviderError("manifest must be a mapping")
    models_raw = raw.get("models")
    if (
        not isinstance(models_raw, Sequence)
        or isinstance(models_raw, (str, bytes))
        or not models_raw
    ):
        raise ProviderError("manifest `models:` must be a non-empty list")
    models: list = []
    for entry in models_raw:
        if not isinstance(entry, Mapping):
            raise ProviderError(f"model entry not a mapping: {entry!r}")
        caps = entry.get("capabilities")
        if (
            not isinstance(caps, Sequence)
            or isinstance(caps, (str, bytes))
            or not caps
        ):
            raise ProviderError(
                f"model {entry.get('id')!r} `capabilities:` must be a non-empty list"
            )
        ctx = entry.get("context")
        # `isinstance(True, int)` is True, so a `context: true` must not become 1.
        context = ctx if isinstance(ctx, int) and not isinstance(ctx, bool) else None
        models.append(
            make_entry(
                id=str(entry.get("id", "")),
                provider=str(entry.get("provider", "")),
                capabilities=[str(c) for c in caps],
                context=context,
                notes=str(entry.get("notes", "")),
            )
        )
    roles = {str(k): str(v) for k, v in dict(raw.get("roles") or {}).items()}
    aliases = {str(k): str(v) for k, v in dict(raw.get("aliases") or {}).items()}
    return build_registry(models, roles=roles, aliases=aliases, validate=validate)


def board_from_seats(seats: Iterable[Mapping[str, str]]) -> Board:
    """Build a :class:`Board` from a list of ``{model, role, name}`` mappings.

    The priority is the LIST ORDER (strongest first) — exactly how a tool's ``board:``
    config or a built-in default board is written. ``role`` and ``name`` are optional;
    ``name`` falls back to the model id's last path segment for a short display label.
    A mapping without a usable ``model`` is a :class:`ProviderError` (an empty board from
    a silently-dropped seat is worse than a loud failure).
    """
    out: list = []
    for entry in seats:
        if not isinstance(entry, Mapping):
            raise ProviderError(f"board seat not a mapping: {entry!r}")
        model = str(entry.get("model", "")).strip()
        if not model:
            raise ProviderError(f"board seat missing 'model': {dict(entry)!r}")
        role = str(entry.get("role", "")).strip()
        name = str(entry.get("name", "")).strip()
        display = name or _default_display(model)
        out.append(BoardSeat(model=model, role=role, display=display))
    return Board(seats=tuple(out))


def _default_display(model: str) -> str:
    """A short display label derived from a model id: the part after the last ``:`` and
    ``/`` (``commandcode:deepseek/deepseek-v4-pro`` -> ``deepseek-v4-pro``)."""
    tail = model.split(":", 1)[1] if ":" in model else model
    return tail.rsplit("/", 1)[-1]
