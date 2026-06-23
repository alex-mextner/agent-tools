"""Tests for research-cli — the multi-provider research/panel tool.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_research_cli.py -q
    # the one manifest-loading test additionally needs pyyaml; it self-skips without it:
    uv run --with pytest --with pyyaml python -m pytest tests/test_research_cli.py -q

Every test is deterministic and network-free. The transport is the network seam, so the
panel engine is driven entirely by an INJECTED StubTransport (no subprocess, no HTTP);
registries/boards are built from in-memory data so no test needs PyYAML except the single
real-manifest smoke test, which ``importorskip``s ``yaml`` exactly like the providers
suite. No sleeps, no global state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# research-cli lives at tools/research-cli/; its package is importable once that dir is on
# the path. Its providers.py adds lib/ for the shared CORE, so this is the only shim.
_TOOL = Path(__file__).resolve().parent.parent / "tools" / "research-cli"
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))

from research_cli.engine import ResearchEngine, synthesize  # noqa: E402
from agenttools_providers import build_registry, make_entry  # noqa: E402
from research_cli.providers import (  # noqa: E402
    DEFAULT_RESEARCH_BOARD,
    PROVIDER_KEY_NAMES,
    Board,
    BoardSeat,
    ModelEntry,
    ProviderError,
    key_cascade_for,
    research_board,
    resolve_seat,
)
from research_cli.transport import (  # noqa: E402
    SeatAnswer,
    StubTransport,
    SubprocessTransport,
    Transport,
    TransportError,
)


# --- A small in-memory registry + board, so most tests need no manifest/YAML ------------


def _registry():
    """A 4-model registry with the research roles wired — built from data, no YAML."""
    return build_registry(
        models=[
            make_entry("opus-x", "anthropic", ["reasoning", "code", "vision"]),
            make_entry("fable-x", "anthropic", ["reasoning", "code", "vision"]),
            make_entry("flash-x", "gemini", ["reasoning", "code"]),
            make_entry("kimi-code", "commandcode", ["code", "reasoning"]),  # no vision
        ],
        roles={
            "analyst": "opus-x",
            "reasoning": "opus-x",
            "skeptic": "fable-x",
            "architect": "fable-x",
            "scout": "flash-x",
            "fast": "flash-x",
            "code": "kimi-code",
            "vision": "opus-x",
        },
    )


def _board():
    return Board(
        seats=(
            BoardSeat("analyst", role="analyst", display="Analyst"),
            BoardSeat("skeptic", role="skeptic", display="Skeptic"),
            BoardSeat("scout", role="scout", display="Scout"),
        )
    )


# --- providers bridge: it REUSES the CORE, not a fork -----------------------------------


def test_default_research_board_is_a_research_lens_not_review():
    # The whole point of research-cli: its lenses are research lenses, NOT review-cli's
    # code-review lenses (correctness/security/tests). Guard that intent.
    lenses = {seat["role"] for seat in DEFAULT_RESEARCH_BOARD}
    assert lenses == {"analyst", "skeptic", "scout"}
    assert "correctness" not in lenses and "security" not in lenses


def test_research_board_builds_via_core_board_from_seats():
    board = research_board()
    assert isinstance(board, Board)
    # List order is priority (CORE contract); strongest first.
    assert [s.display for s in board.seats] == ["Analyst", "Skeptic", "Scout"]


def test_resolve_seat_uses_core_role_resolution():
    registry = _registry()
    seat = BoardSeat("analyst", role="analyst", display="Analyst")
    entry = resolve_seat(registry, seat)
    assert isinstance(entry, ModelEntry)
    assert entry.id == "opus-x"
    assert entry.provider == "anthropic"


def test_resolve_seat_unknown_role_raises_provider_error():
    registry = _registry()
    with pytest.raises(ProviderError):
        resolve_seat(registry, BoardSeat("nope", role="x", display="X"))


def test_key_cascade_for_known_and_unknown_provider():
    cascade = key_cascade_for("anthropic")
    assert cascade is not None
    # Reuses the CORE KeyCascade verbatim: env beats files, name precedence ordered.
    assert cascade.resolve(env={"ANTHROPIC_API_KEY": "sk-1"}) == "sk-1"
    # Alias name is honored too (declared after the canonical name).
    assert cascade.resolve(env={"CLAUDE_API_KEY": "sk-2"}) == "sk-2"
    assert key_cascade_for("no-such-provider") is None


def test_every_default_board_provider_has_key_names():
    # A seat whose provider has no key names can never be reachable — catch a board/keymap
    # drift where a default seat resolves to a provider absent from PROVIDER_KEY_NAMES.
    # Resolve EVERY seat first (collecting any failures) so one unresolvable seat does not
    # mask drift on the others; only skip if the registry can resolve NOTHING.
    registry = _build_real_or_inmemory_registry()
    resolved = []
    unresolved = []
    for seat in research_board().seats:
        try:
            resolved.append((seat, resolve_seat(registry, seat)))
        except ProviderError:
            unresolved.append(seat)
    if not resolved:
        pytest.skip("manifest unavailable; covered by the in-memory engine tests")
    assert not unresolved, f"seats failed to resolve: {[s.display for s in unresolved]}"
    for seat, entry in resolved:
        assert entry.provider in PROVIDER_KEY_NAMES, (
            f"seat {seat.display} -> provider {entry.provider} has no key names"
        )


def _build_real_or_inmemory_registry():
    # Prefer the real manifest if pyyaml is present; otherwise a representative in-memory
    # registry whose providers match the default board's resolved providers.
    try:
        import yaml  # noqa: F401

        from research_cli.providers import load_research_registry

        return load_research_registry()
    except Exception:
        return build_registry(
            models=[
                make_entry("opus-x", "anthropic", ["reasoning", "code", "vision"]),
                make_entry("fable-x", "anthropic", ["reasoning", "code", "vision"]),
                make_entry("flash-x", "gemini", ["reasoning", "code"]),
            ],
            roles={
                "reasoning": "opus-x",
                "architect": "fable-x",
                "fast": "flash-x",
            },
        )


# --- transport: reachability via the CORE key cascade -----------------------------------


def test_subprocess_transport_reachability_uses_key_cascade():
    t = SubprocessTransport(env={"ANTHROPIC_API_KEY": "sk-real"})
    anthropic = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    gemini = ModelEntry("g", "gemini", frozenset({"reasoning"}))
    assert t.is_reachable(anthropic) is True   # has a key
    assert t.is_reachable(gemini) is False     # no GEMINI key in env


def test_subprocess_transport_unknown_provider_unreachable():
    t = SubprocessTransport(env={"ANTHROPIC_API_KEY": "sk"})
    weird = ModelEntry("x", "no-such-provider", frozenset({"reasoning"}))
    assert t.is_reachable(weird) is False


def test_subprocess_transport_no_backend_raises():
    t = SubprocessTransport(env={"ANTHROPIC_API_KEY": "sk"}, command_template=None)
    entry = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    with pytest.raises(TransportError):
        t.ask(entry, question="q", lens="analyst", timeout=1.0)


def test_subprocess_transport_runs_command_template(tmp_path):
    # A real subprocess (echo) — proves the backend path actually shells out and returns
    # stdout, without any network. The template is operator-supplied config.
    t = SubprocessTransport(
        env={"ANTHROPIC_API_KEY": "sk"},
        command_template="printf 'answer for %s' {model}",
    )
    entry = ModelEntry("opus", "anthropic", frozenset({"reasoning"}))
    out = t.ask(entry, question="q", lens="analyst", timeout=10.0)
    assert "answer for" in out and "opus" in out


def test_subprocess_transport_template_preserves_shell_var():
    # A template containing a shell ${VAR} must NOT crash (str.format would KeyError on it);
    # only our {model}/{lens}/{question} placeholders are substituted, ${VAR} is left for
    # the shell. Echo the resolved env var back to prove the shell expanded it.
    t = SubprocessTransport(
        env={"ANTHROPIC_API_KEY": "sk", "MYTOKEN": "tok-123"},
        command_template='printf "%s for {model}" "${MYTOKEN}"',
    )
    entry = ModelEntry("opus", "anthropic", frozenset({"reasoning"}))
    # The subprocess inherits the parent env, so set it for this call's shell too.
    import os

    os.environ["MYTOKEN"] = "tok-123"
    try:
        out = t.ask(entry, question="q", lens="analyst", timeout=10.0)
    finally:
        os.environ.pop("MYTOKEN", None)
    assert "tok-123 for" in out and "opus" in out


def test_subprocess_transport_nonzero_exit_raises_transport_error():
    t = SubprocessTransport(
        env={"ANTHROPIC_API_KEY": "sk"},
        command_template="echo 'boom' >&2; exit 3",
    )
    entry = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    with pytest.raises(TransportError) as exc:
        t.ask(entry, question="q", lens="analyst", timeout=10.0)
    assert "boom" in str(exc.value) or "exit" in str(exc.value)


def test_subprocess_transport_empty_output_raises_transport_error():
    t = SubprocessTransport(
        env={"ANTHROPIC_API_KEY": "sk"},
        command_template="true",  # exit 0, no stdout
    )
    entry = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    with pytest.raises(TransportError):
        t.ask(entry, question="q", lens="analyst", timeout=10.0)


def test_subprocess_transport_timeout_raises_transport_error():
    t = SubprocessTransport(
        env={"ANTHROPIC_API_KEY": "sk"},
        command_template="sleep 5",
    )
    entry = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    with pytest.raises(TransportError) as exc:
        t.ask(entry, question="q", lens="analyst", timeout=0.2)
    assert "timed out" in str(exc.value)


def test_subprocess_transport_reachable_via_dotenv_file(tmp_path):
    # Default dotenv fallback: with no env var but a .env file holding the key, the seat is
    # reachable (the documented "env beats .env files" cascade actually reads the file).
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-from-file\n", encoding="utf-8")
    t = SubprocessTransport(env={}, dotenv_files=(str(env_file),))
    entry = ModelEntry("m", "anthropic", frozenset({"reasoning"}))
    assert t.is_reachable(entry) is True


def test_stub_transport_records_calls_and_returns_canned():
    t = StubTransport(answers={"opus-x": "canned opus answer"})
    entry = ModelEntry("opus-x", "anthropic", frozenset({"reasoning"}))
    out = t.ask(entry, question="Q?", lens="analyst", timeout=1.0)
    assert out == "canned opus answer"
    assert t.calls == [{"model": "opus-x", "lens": "analyst", "question": "Q?"}]
    assert isinstance(t, Transport)  # satisfies the runtime-checkable protocol


# --- engine: the single-round panel pass ------------------------------------------------


def test_engine_runs_full_panel_and_synthesizes():
    t = StubTransport(
        answers={
            "opus-x": "Monorepos centralize tooling.",
            "fable-x": "But they couple release cadence.",
            "flash-x": "Tradeoff: simplicity vs blast radius.",
        }
    )
    engine = ResearchEngine(transport=t, registry=_registry(), board=_board())
    result = engine.run("What are the trade-offs of monorepos?")

    assert len(result.answered) == 3
    assert not result.failed
    # Every pooled seat was asked, attributed to its concrete model in the synthesis.
    for model_id in ("opus-x", "fable-x", "flash-x"):
        assert model_id in result.synthesis
    assert "Monorepos centralize tooling." in result.synthesis
    assert result.synthesis.startswith("# Research:")


def test_engine_pool_size_limits_seats_asked():
    t = StubTransport()
    engine = ResearchEngine(transport=t, registry=_registry(), board=_board(), pool_size=2)
    result = engine.run("q")
    assert len(result.answered) == 2  # only the top-2 reachable seats
    assert len(t.calls) == 2


def test_engine_skips_unreachable_seat_and_promotes_reserve():
    # The middle seat (fable-x) is unreachable; the CORE board predicate skips it and the
    # reserve (scout/flash-x) is promoted, so pool_size=2 still yields 2 ANSWERS.
    def reachable(entry: ModelEntry) -> bool:
        return entry.id != "fable-x"

    t = StubTransport(reachable=reachable)
    engine = ResearchEngine(transport=t, registry=_registry(), board=_board(), pool_size=2)
    result = engine.run("q")
    answered_ids = {a.model_id for a in result.answered}
    assert answered_ids == {"opus-x", "flash-x"}  # fable-x skipped at reachability
    assert "fable-x" not in {c["model"] for c in t.calls}


def test_engine_midrun_call_failure_backfills_from_reserve():
    # A seat passes reachability but FAILS at call time → the engine promotes the next
    # reserve seat (mid-run failover). pool_size=2 over a 3-seat board: opus fails the
    # call, scout (reserve) backfills, so we still get 2 successful answers + 1 recorded
    # failure.
    class FlakyOpus(StubTransport):
        def ask(self, entry, *, question, lens, timeout):
            if entry.id == "opus-x":
                raise TransportError("opus-x: simulated mid-run failure")
            return super().ask(entry, question=question, lens=lens, timeout=timeout)

    t = FlakyOpus()
    engine = ResearchEngine(transport=t, registry=_registry(), board=_board(), pool_size=2)
    result = engine.run("q")

    failed_ids = {a.model_id for a in result.failed}
    answered_ids = {a.model_id for a in result.answered}
    assert "opus-x" in failed_ids
    # skeptic was pooled, scout was promoted from reserve to backfill opus.
    assert answered_ids == {"fable-x", "flash-x"}
    assert len(result.answered) == 2


def test_engine_pool_size_zero_or_negative_asks_all_reachable():
    # The CORE's split treats pool <= 0 as "all available" — documented as "<=0 means all
    # reachable". Guard that contract through the engine.
    t = StubTransport()
    for ps in (0, -1):
        engine = ResearchEngine(
            transport=t, registry=_registry(), board=_board(), pool_size=ps
        )
        result = engine.run("q")
        assert len(result.answered) == 3  # the whole board


def test_engine_records_non_transport_error_without_crashing():
    # A transport (custom or live) that raises something OTHER than TransportError (e.g.
    # OSError from a missing binary) must be recorded as a failed seat, not abort the run.
    class Exploding(StubTransport):
        def ask(self, entry, *, question, lens, timeout):
            if entry.id == "opus-x":
                raise OSError("binary not found")
            return super().ask(entry, question=question, lens=lens, timeout=timeout)

    engine = ResearchEngine(
        transport=Exploding(), registry=_registry(), board=_board(), pool_size=1
    )
    result = engine.run("q")
    # opus-x failed (OSError, recorded), the reserve backfilled, so we still synthesize.
    failed = {a.model_id for a in result.failed}
    assert "opus-x" in failed
    assert any("OSError" in a.error for a in result.failed)
    assert result.answered  # backfilled, run did not crash


def test_engine_no_reachable_seats_yields_empty_synthesis():
    t = StubTransport(reachable=lambda _e: False)
    engine = ResearchEngine(transport=t, registry=_registry(), board=_board())
    result = engine.run("q")
    assert result.answered == []
    assert "No seat produced an answer" in result.synthesis


def test_engine_rejects_empty_question():
    engine = ResearchEngine(transport=StubTransport(), registry=_registry(), board=_board())
    with pytest.raises(ValueError):
        engine.run("   ")


# --- synthesis formatting ---------------------------------------------------------------


def test_synthesize_attributes_each_answer_and_notes_failures():
    answers = [
        SeatAnswer(
            seat=BoardSeat("analyst", role="analyst", display="Analyst"),
            model_id="opus-x",
            provider="anthropic",
            ok=True,
            text="Answer A.",
        ),
        SeatAnswer(
            seat=BoardSeat("scout", role="scout", display="Scout"),
            model_id="flash-x",
            provider="gemini",
            ok=False,
            error="no key",
        ),
    ]
    out = synthesize("Q?", answers)
    assert "## Analyst · analyst  (opus-x)" in out
    assert "Answer A." in out
    assert "1 model answered; 1 unavailable" in out
    assert "Scout unavailable: no key" in out


# --- machine-readable JSON rendering (the --json output path) ----------------------------


def test_render_json_serializes_every_seat_with_full_shape():
    # The structured counterpart of synthesize(): each seat becomes one object carrying the
    # data a downstream consumer needs (display/lens/model/provider/ok/text/error), and the
    # top-level run carries the question + the answered/failed counts. No Markdown scraping.
    import json

    from research_cli.engine import render_json

    answers = [
        SeatAnswer(
            seat=BoardSeat("analyst", role="analyst", display="Analyst"),
            model_id="opus-x",
            provider="anthropic",
            ok=True,
            text="Answer A.",
        ),
        SeatAnswer(
            seat=BoardSeat("scout", role="scout", display="Scout"),
            model_id="flash-x",
            provider="gemini",
            ok=False,
            error="no key",
        ),
    ]
    payload = json.loads(render_json("Q?", answers))

    assert payload["question"] == "Q?"
    assert payload["answered"] == 1
    assert payload["failed"] == 1
    assert len(payload["seats"]) == 2

    analyst = payload["seats"][0]
    assert analyst == {
        "display": "Analyst",
        "lens": "analyst",
        "model": "opus-x",
        "provider": "anthropic",
        "ok": True,
        "text": "Answer A.",
        "error": "",
    }
    scout = payload["seats"][1]
    assert scout["ok"] is False
    assert scout["error"] == "no key"
    assert scout["text"] == ""


def test_render_json_empty_panel_is_valid_json_with_zero_counts():
    import json

    from research_cli.engine import render_json

    payload = json.loads(render_json("Q?", []))
    assert payload == {"question": "Q?", "answered": 0, "failed": 0, "seats": []}


# --- CLI dispatcher + ask command -------------------------------------------------------


def test_cli_discovers_self_registering_commands():
    from research_cli.cli import _discover

    commands = _discover()
    assert "ask" in commands
    assert "board" in commands


def test_cli_version_and_help(capsys):
    from research_cli.cli import main

    assert main(["--version"]) == 0
    assert "research 0.1.0" in capsys.readouterr().out

    assert main(["--help"]) == 0
    assert "Commands:" in capsys.readouterr().out


def test_cli_unknown_command_is_unknown_item_error(capsys):
    # research-cli now routes a diagnosed failure through the shared agenttools_errors layer,
    # which classifies an unknown command as the precise UNKNOWN_ITEM class (4) — distinct from
    # a plain bad-flag usage error (2) — and renders the 3-part what/why/fix block.
    from research_cli.cli import main

    from agenttools_errors import EXIT_UNKNOWN_ITEM

    assert main(["frobnicate"]) == EXIT_UNKNOWN_ITEM
    err = capsys.readouterr().err
    assert "unknown command" in err  # the WHAT line
    assert "fix:" in err  # the HOW-to-fix line of the shared block


def test_command_help_exits_zero(capsys):
    # argparse raises SystemExit(0) for --help; the command's handler must return 0, not the
    # usage code (the `exc.code or 2` falsy-zero trap).
    from research_cli.commands.ask import run as ask_run
    from research_cli.commands.board import run as board_run

    assert ask_run(["--help"]) == 0
    assert "research ask" in capsys.readouterr().out
    assert board_run(["--help"]) == 0
    assert "research board" in capsys.readouterr().out


def test_board_command_runs_against_real_manifest(capsys):
    pytest.importorskip("yaml")
    from research_cli.commands.board import run as board_run

    assert board_run([]) == 0
    out = capsys.readouterr().out
    assert "Research board" in out
    # Each default seat resolves to a concrete model with its provider shown.
    assert "Analyst" in out and "[anthropic]" in out


def test_ask_command_offline_runs_end_to_end(capsys):
    # The offline path uses the StubTransport + the real default board resolved against
    # the in-memory fallback... but the command builds its own engine, which loads the
    # real manifest. So this test only runs when the manifest is loadable (pyyaml). It is
    # the one true end-to-end-through-the-CLI smoke test.
    pytest.importorskip("yaml")
    from research_cli.commands.ask import run

    code = run(["--offline", "What are the trade-offs of monorepos?"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("# Research:")
    assert "Panel:" in out


def test_ask_command_empty_question_usage_error(capsys):
    from research_cli.commands.ask import run

    assert run([]) == 2  # argparse: nargs="+" requires at least one


def test_ask_command_json_flag_emits_valid_json(capsys):
    # The CLI flag end-to-end: --json swaps the Markdown note for a JSON object. Needs the
    # real manifest (the command builds its own engine), so it self-skips without pyyaml,
    # exactly like the Markdown offline smoke test above.
    import json

    pytest.importorskip("yaml")
    from research_cli.commands.ask import run

    code = run(["--offline", "--json", "What are the trade-offs of monorepos?"])
    out = capsys.readouterr().out
    assert code == 0

    payload = json.loads(out)  # must be parseable, not Markdown
    assert payload["question"] == "What are the trade-offs of monorepos?"
    assert payload["answered"] >= 1
    assert payload["seats"]
    for seat in payload["seats"]:
        assert set(seat) == {"display", "lens", "model", "provider", "ok", "text", "error"}


def test_ask_command_json_no_seat_answered_still_exits_network_and_prints_json(
    tmp_path, monkeypatch, capsys
):
    # The structured-exit-code contract holds under --json: no reachable seat still exits 7
    # (EXIT_NETWORK), and a script still gets a parseable object (answered=0) BEFORE the
    # error block — so it can branch on the exit code AND read the empty payload.
    import json
    import os

    pytest.importorskip("yaml")
    from research_cli.commands.ask import run

    # --offline reaches every seat by default; force "no seat answered" deterministically.
    # Reachability = the key cascade resolving a key (env vars, then a `.env` file in CWD).
    # Drop every *_API_KEY from the env AND run from an empty dir (no `.env`), so the cascade
    # has nothing to resolve and EVERY seat is unreachable — no source-of-key dependence.
    for key in [k for k in os.environ if k.endswith("_API_KEY")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    code = run(["--json", "research question with no backend"])
    out = capsys.readouterr().out
    assert code == 7  # EXIT_NETWORK — nothing reachable
    payload = json.loads(out)
    assert payload["answered"] == 0


def test_ask_command_without_json_is_unchanged_markdown(capsys):
    # Regression guard: the default (no --json) output stays Markdown, byte-shape unchanged.
    pytest.importorskip("yaml")
    from research_cli.commands.ask import run

    code = run(["--offline", "monorepo trade-offs?"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("# Research:")
    assert "Panel:" in out
    # Not JSON: a Markdown note does not parse as a JSON object.
    import json

    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_load_real_manifest_resolves_research_board():
    # Smoke test the REAL ecosystem manifest path (the providers-engine reuse against
    # lib/contracts/models.yaml). Needs pyyaml; self-skips otherwise (same as the
    # providers suite). Proves every default research seat resolves against the shipped
    # manifest, not just an in-memory fixture.
    pytest.importorskip("yaml")
    from research_cli.providers import load_research_registry

    registry = load_research_registry()
    for seat in research_board().seats:
        entry = resolve_seat(registry, seat)
        assert entry.id  # resolves to a concrete pin
        assert entry.provider in PROVIDER_KEY_NAMES
