"""Tests for agenttools_providers — the tool-agnostic provider-abstraction CORE.

Run from the repo root::

    uv run --with pytest --with pyyaml python -m pytest tests/test_agenttools_providers.py -q
    # or, if agenttools-providers[test] is installed:  python -m pytest tests/ -q

Every test is deterministic and self-contained: registries/boards are built from
in-memory data, the key cascade is resolved against an INJECTED env mapping + reader (no
os.environ, no disk), and the one filesystem touch (the YAML manifest path) uses pytest's
``tmp_path``. No network, no sleeps, no global state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_providers as ap  # noqa: E402
from agenttools_providers import (  # noqa: E402
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
    registry_from_mapping,
    resolve_role,
    validate_registry,
)


# --- Fixtures ------------------------------------------------------------------------
def _registry() -> Registry:
    """A small, priority-ordered registry mirroring the ecosystem's real shape:
    some vision-capable models, some code/reasoning-only (no vision)."""
    return build_registry(
        models=[
            make_entry("claude-fable-5", "anthropic", ["vision", "reasoning", "code"]),
            make_entry("claude-opus-4-8", "anthropic", ["vision", "reasoning", "code"]),
            make_entry("kimi-k2.7-code", "commandcode", ["code", "reasoning"]),  # NO vision
            make_entry("glm-5.2", "zai", ["reasoning", "code"]),                  # NO vision
            make_entry("kimi-k2p6-turbo", "commandcode", ["vision", "code", "reasoning"]),
        ],
        roles={
            "architect": "claude-fable-5",
            "reasoning": "claude-opus-4-8",
            "code": "kimi-k2.7-code",
            "vision": "kimi-k2p6-turbo",  # MUST be vision-capable
        },
        aliases={
            "anthropic:latest": "claude-fable-5",
            "zai:latest": "glm-5.2",
        },
    )


# --- Capability vocabulary -----------------------------------------------------------
def test_capability_normalises_and_rejects_unknown():
    assert Capability("VISION") == "vision"
    assert Capability(" Code ") == "code"
    assert Capability.VISION == "vision"
    with pytest.raises(ProviderError):
        Capability("telepathy")


def test_model_entry_has_is_case_insensitive():
    entry = make_entry("m", "p", ["Vision", "code"])
    assert entry.has("vision")
    assert entry.has("VISION")
    assert not entry.has("reasoning")


def test_make_entry_rejects_empty_and_unknown_caps():
    with pytest.raises(ProviderError):
        make_entry("", "p", ["vision"])
    with pytest.raises(ProviderError):
        make_entry("m", "", ["vision"])
    with pytest.raises(ProviderError):
        make_entry("m", "p", [])
    with pytest.raises(ProviderError):
        make_entry("m", "p", ["not-a-cap"])


# --- Capability-tag filtering --------------------------------------------------------
def test_with_capability_returns_only_tagged_entries_in_priority_order():
    reg = _registry()
    vision = [m.id for m in reg.with_capability("vision")]
    # Exactly the three vision-tagged ids, in registry (priority) order; the
    # code/reasoning-only models are excluded.
    assert vision == ["claude-fable-5", "claude-opus-4-8", "kimi-k2p6-turbo"]
    assert "kimi-k2.7-code" not in vision
    assert "glm-5.2" not in vision


def test_with_capability_code_includes_the_code_only_models():
    reg = _registry()
    code = {m.id for m in reg.with_capability("code")}
    assert "kimi-k2.7-code" in code
    assert "glm-5.2" in code


def test_with_capability_unknown_raises_not_empty_list():
    # A typo'd capability must fail loud, not silently return [] (which reads as
    # "no such models" and would hide the bug).
    with pytest.raises(ProviderError):
        _registry().with_capability("visoin")


def test_registry_queries_by_provider_and_id():
    reg = _registry()
    assert [m.id for m in reg.by_provider("anthropic")] == [
        "claude-fable-5",
        "claude-opus-4-8",
    ]
    assert reg.providers() == ["anthropic", "commandcode", "zai"]
    assert reg.entry("glm-5.2").provider == "zai"
    assert reg.entry("does-not-exist") is None


# --- Role resolution honoring tags ---------------------------------------------------
def test_resolve_role_maps_symbolic_to_concrete_entry():
    reg = _registry()
    assert resolve_role(reg, "architect").id == "claude-fable-5"
    assert resolve_role(reg, "reasoning").id == "claude-opus-4-8"
    assert resolve_role(reg, "code").id == "kimi-k2.7-code"


def test_resolve_role_vision_resolves_only_to_vision_capable():
    reg = _registry()
    chosen = resolve_role(reg, "vision")
    assert chosen.id == "kimi-k2p6-turbo"
    assert chosen.has("vision")


def test_resolve_role_falls_through_to_aliases():
    reg = _registry()
    assert resolve_role(reg, "anthropic:latest").id == "claude-fable-5"
    assert resolve_role(reg, "zai:latest").id == "glm-5.2"


def test_resolve_role_accepts_a_concrete_id_directly():
    reg = _registry()
    assert resolve_role(reg, "glm-5.2").id == "glm-5.2"


def test_resolve_role_unknown_raises():
    with pytest.raises(ProviderError):
        resolve_role(_registry(), "nonexistent-role")


def test_resolve_role_require_capability_is_enforced():
    reg = _registry()
    # 'code' role -> kimi-k2.7-code, which is NOT vision-capable. Demanding vision fails.
    with pytest.raises(ProviderError):
        resolve_role(reg, "code", require_capability="vision")
    # But the same role resolves fine when we demand a capability it has.
    assert resolve_role(reg, "code", require_capability="code").id == "kimi-k2.7-code"


def test_vision_role_pointing_at_text_only_model_is_rejected_at_build_time():
    # A misconfigured `vision: <text-only model>` must be a loud build-time error, not a
    # silent wrong pick when --visual later filters.
    with pytest.raises(ProviderError) as exc:
        build_registry(
            models=[
                make_entry("kimi-k2.7-code", "commandcode", ["code", "reasoning"]),
            ],
            roles={"vision": "kimi-k2.7-code"},  # not vision-capable!
        )
    assert "vision" in str(exc.value)


def test_resolve_role_enforces_vision_even_when_build_validation_skipped():
    # A registry built with validate=False still cannot hand back a text-only model for
    # the `vision` role — the guard is re-applied at resolve time.
    reg = build_registry(
        models=[make_entry("kimi-k2.7-code", "commandcode", ["code"])],
        roles={"vision": "kimi-k2.7-code"},
        validate=False,
    )
    with pytest.raises(ProviderError):
        resolve_role(reg, "vision")


# --- Registry validation -------------------------------------------------------------
def test_validate_registry_catches_dangling_role_and_alias_targets():
    reg = Registry(
        models=(make_entry("m1", "p", ["code"]),),
        roles={"code": "missing-model"},
        aliases={"p:latest": "also-missing"},
    )
    problems = validate_registry(reg)
    assert any("missing-model" in p for p in problems)
    assert any("also-missing" in p for p in problems)


def test_validate_registry_catches_provider_latest_mismatch():
    reg = Registry(
        models=(
            make_entry("m1", "anthropic", ["code"]),
            make_entry("m2", "zai", ["code"]),
        ),
        aliases={"anthropic:latest": "m2"},  # m2 is a zai model, not anthropic
    )
    problems = validate_registry(reg)
    assert any("anthropic" in p and "zai" in p for p in problems)


def test_validate_registry_catches_duplicate_ids():
    reg = Registry(
        models=(
            make_entry("dup", "p", ["code"]),
            make_entry("dup", "p", ["reasoning"]),
        ),
    )
    assert any("duplicate" in p for p in validate_registry(reg))


def test_valid_registry_has_no_problems():
    assert validate_registry(_registry()) == []


# --- Failover ordering ---------------------------------------------------------------
def _board() -> Board:
    return Board(
        seats=(
            BoardSeat("claude-fable-5", role="architect", display="Fable"),
            BoardSeat("claude-opus-4-8", role="correctness", display="Opus"),
            BoardSeat("codex", role="consistency", display="Codex"),
            BoardSeat("kimi-k2.7-code", role="performance", display="Kimi"),
            BoardSeat("glm-5.2", role="quality", display="GLM"),
        )
    )


def test_pool_takes_top_n_in_priority_order():
    board = _board()
    assert [s.display for s in board.pool(3)] == ["Fable", "Opus", "Codex"]


def test_pool_zero_or_negative_means_all():
    board = _board()
    assert len(board.pool(0)) == 5
    assert len(board.pool(-1)) == 5


def test_pool_larger_than_count_is_clamped():
    board = _board()
    assert len(board.pool(99)) == 5


def test_split_partitions_into_pool_and_reserve_disjoint_and_ordered():
    board = _board()
    pool, reserve = board.split(2)
    assert [s.display for s in pool] == ["Fable", "Opus"]
    assert [s.display for s in reserve] == ["Codex", "Kimi", "GLM"]
    # disjoint + together == every seat, in priority order
    assert [s.display for s in pool + reserve] == [s.display for s in board.seats]


def test_startup_failover_skips_unavailable_and_promotes_reserve():
    board = _board()
    # Fable + Codex unreachable at startup → skipped; the next-priority reachable seats
    # are pulled up to keep a full pool of 3.
    dead = {"claude-fable-5", "codex"}

    def available(seat: BoardSeat) -> bool:
        return seat.model not in dead

    pool = board.pool(3, available)
    assert [s.display for s in pool] == ["Opus", "Kimi", "GLM"]
    # An unavailable seat is in NEITHER list of the split.
    pool2, reserve2 = board.split(3, available)
    seen = {s.model for s in pool2 + reserve2}
    assert dead.isdisjoint(seen)


def test_failover_order_is_the_available_seats_in_priority_order():
    board = _board()

    def available(seat: BoardSeat) -> bool:
        return seat.model != "claude-opus-4-8"

    order = [s.display for s in failover_order(board, available)]
    assert order == ["Fable", "Codex", "Kimi", "GLM"]


def test_reserve_promotion_keeps_each_seats_own_lens():
    # The lens travels WITH the seat: a promoted reserve brings its own role, it does not
    # inherit the skipped seat's role.
    board = _board()

    def available(seat: BoardSeat) -> bool:
        return seat.model != "claude-fable-5"

    promoted = board.pool(1, available)[0]
    assert promoted.display == "Opus"
    assert promoted.role == "correctness"  # its own lens, not Fable's "architect"


def test_board_from_seats_orders_by_list_and_defaults_display():
    board = board_from_seats(
        [
            {"model": "claude:claude-opus-4-8", "role": "correctness", "name": "Opus"},
            {"model": "commandcode:deepseek/deepseek-v4-pro", "role": "tests"},
        ]
    )
    assert board.seats[0].display == "Opus"
    # No explicit name → last path segment of the id.
    assert board.seats[1].display == "deepseek-v4-pro"
    assert board.seats[1].role == "tests"


def test_board_from_seats_rejects_seat_without_model():
    with pytest.raises(ProviderError):
        board_from_seats([{"role": "tests"}])


# --- Key cascade ---------------------------------------------------------------------
def test_key_cascade_env_var_wins_over_files():
    cascade = KeyCascade(names=("ANTHROPIC_API_KEY",), files=(Path("/some/.env"),))

    def reader(_path: Path, _var: str) -> str:
        return "from-file"

    got = cascade.resolve(env={"ANTHROPIC_API_KEY": "from-env"}, reader=reader)
    assert got == "from-env"


def test_key_cascade_env_name_order_first_set_wins():
    cascade = KeyCascade(names=("PRIMARY", "ALIAS"))
    # Both set; the canonical/primary name (declared first) wins.
    assert cascade.resolve(env={"PRIMARY": "p", "ALIAS": "a"}) == "p"
    # Only the alias set → the alias resolves.
    assert cascade.resolve(env={"ALIAS": "a"}) == "a"


def test_key_cascade_blank_env_value_is_skipped():
    cascade = KeyCascade(names=("PRIMARY", "ALIAS"))
    # PRIMARY present but empty/whitespace → skip it, fall through to ALIAS.
    assert cascade.resolve(env={"PRIMARY": "   ", "ALIAS": "a"}) == "a"


def test_key_cascade_falls_back_to_files_when_env_empty():
    f1, f2 = Path("/first/.env"), Path("/second/.env")
    cascade = KeyCascade(names=("PRIMARY",), files=(f1, f2))
    store = {(f2, "PRIMARY"): "in-second-file"}

    def reader(path: Path, var: str):
        return store.get((path, var))

    assert cascade.resolve(env={}, reader=reader) == "in-second-file"


def test_key_cascade_name_priority_beats_file_priority():
    # The CANONICAL key name in a LATER file beats an ALIAS in an EARLIER file: precedence
    # is key-name-first, not path-first (review-cli's _resolve_key invariant).
    early, late = Path("/early/.env"), Path("/late/.env")
    cascade = KeyCascade(names=("PRIMARY", "ALIAS"), files=(early, late))
    store = {
        (early, "ALIAS"): "alias-in-early",
        (late, "PRIMARY"): "primary-in-late",
    }

    def reader(path: Path, var: str):
        return store.get((path, var))

    # Even though early/ALIAS is found in an earlier file, PRIMARY (the canonical name)
    # is checked across ALL files first → primary-in-late wins.
    assert cascade.resolve(env={}, reader=reader) == "primary-in-late"


def test_key_cascade_returns_none_when_nothing_resolves():
    cascade = KeyCascade(names=("PRIMARY",), files=(Path("/x/.env"),))
    assert cascade.resolve(env={}, reader=lambda _p, _v: None) is None


def test_key_cascade_requires_at_least_one_name():
    with pytest.raises(ProviderError):
        KeyCascade(names=())


def test_read_dotenv_value_parses_and_skips_missing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'FOO="quoted-value"\nBAR=plain\n# comment\nEMPTY=\n', encoding="utf-8"
    )
    assert ap.read_dotenv_value(env_file, "FOO") == "quoted-value"
    assert ap.read_dotenv_value(env_file, "BAR") == "plain"
    assert ap.read_dotenv_value(env_file, "EMPTY") is None
    assert ap.read_dotenv_value(env_file, "MISSING") is None
    # An unreadable file is a skip, not a crash.
    assert ap.read_dotenv_value(tmp_path / "nope.env", "FOO") is None


def test_key_cascade_end_to_end_with_real_dotenv_reader(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=key-from-disk\n", encoding="utf-8")
    cascade = KeyCascade(names=("GEMINI_API_KEY", "GOOGLE_API_KEY"), files=(env_file,))
    # Empty env → falls back to the real dotenv reader against the tmp file.
    assert cascade.resolve(env={}) == "key-from-disk"


# --- Manifest loading ----------------------------------------------------------------
_MANIFEST_TEXT = """\
version: 1
models:
  - id: claude-opus-4-8
    provider: anthropic
    capabilities: [vision, reasoning, code]
    context: 200000
    notes: "Opus 4.8"
  - id: kimi-k2.7-code
    provider: commandcode
    capabilities: [code, reasoning]
  - id: kimi-k2p6-turbo
    provider: commandcode
    capabilities: [vision, code, reasoning]
roles:
  reasoning: claude-opus-4-8
  code: kimi-k2.7-code
  vision: kimi-k2p6-turbo
aliases:
  anthropic:latest: claude-opus-4-8
"""


def test_load_registry_from_yaml_file(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "models.yaml"
    path.write_text(_MANIFEST_TEXT, encoding="utf-8")
    reg = load_registry(path)
    assert reg.entry("claude-opus-4-8").context == 200000
    assert {m.id for m in reg.with_capability("vision")} == {
        "claude-opus-4-8",
        "kimi-k2p6-turbo",
    }
    assert resolve_role(reg, "vision").id == "kimi-k2p6-turbo"


def test_load_registry_rejects_vision_role_on_text_only_model(tmp_path):
    pytest.importorskip("yaml")
    bad = _MANIFEST_TEXT.replace("vision: kimi-k2p6-turbo", "vision: kimi-k2.7-code")
    path = tmp_path / "bad.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ProviderError):
        load_registry(path)


def test_registry_from_mapping_rejects_bad_shapes():
    with pytest.raises(ProviderError):
        registry_from_mapping("not a mapping")
    with pytest.raises(ProviderError):
        registry_from_mapping({"models": []})  # empty models list
    with pytest.raises(ProviderError):
        registry_from_mapping({"models": [{"id": "m", "provider": "p"}]})  # no caps


def test_registry_from_mapping_guards_against_context_true():
    # `context: true` must not silently become 1 (isinstance(True, int) is True).
    reg = registry_from_mapping(
        {
            "models": [
                {"id": "m", "provider": "p", "capabilities": ["code"], "context": True}
            ]
        }
    )
    assert reg.entry("m").context is None


def test_module_exposes_version_and_all():
    assert isinstance(ap.__version__, str)
    for name in ap.__all__:
        assert hasattr(ap, name)


def test_models_entry_is_immutable():
    entry = make_entry("m", "p", ["code"])
    assert isinstance(entry, ModelEntry)
    with pytest.raises((AttributeError, TypeError)):
        entry.id = "other"  # frozen dataclass
