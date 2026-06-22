"""Tests for research-cli's adoption of the shared ``agenttools_errors`` layer.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_research_cli_errors.py -q

These cover the CLI's USER-FACING error surface (dispatcher + the ``ask``/``board``
commands): every COMMAND-BODY failure (the tool's own diagnosed errors — unknown command,
empty question, unreachable board, malformed manifest, a broken command module) must render
the shared three-part block (WHAT / WHY / HOW-to-fix) and exit with the right
``agenttools_errors`` class code, not a bare ``print`` + an ad-hoc number.

SCOPE NOTE: argparse's OWN usage errors (an unknown/invalid flag) are deliberately left to
argparse — it prints its terse usage line and the standard exit ``2`` (``EXIT_USAGE``), the
conventional CLI behavior. The shared block covers the errors the command code raises itself,
not argparse's flag-parsing diagnostics.

Deterministic and network-free — ``ask`` runs against the stub transport (``--offline``) or
against a manifest path that doesn't exist, so no key, no subprocess, no HTTP.
"""

from __future__ import annotations

import sys
from pathlib import Path

# research-cli lives at tools/research-cli/; put it on the path. Its providers.py adds lib/ so
# the shared agenttools_errors / agenttools_providers resolve from the source checkout.
_TOOL = Path(__file__).resolve().parent.parent / "tools" / "research-cli"
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))

# Importing providers first runs its lib/ sys.path shim, so agenttools_errors is importable.
from research_cli import providers as _providers  # noqa: E402,F401
from research_cli.cli import main  # noqa: E402
from research_cli.commands import ask as ask_cmd  # noqa: E402
from research_cli.commands import board as board_cmd  # noqa: E402
from research_cli.providers import PROVIDER_KEY_NAMES  # noqa: E402

# Import the exit-code constants from research-cli's OWN one-import-point shim (which
# re-exports the shared agenttools_errors codes), the same place the production code uses.
from research_cli._errors import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_NETWORK,
    EXIT_UNKNOWN_ITEM,
    EXIT_USAGE,
)


def _capture(fn, *args):
    """Run ``fn(*args)`` and return (exit_code, stderr_text)."""
    import io
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, err.getvalue()


# ── dispatcher: unknown command → did-you-mean + EXIT_USAGE ──────────────────────────────


def test_unknown_command_renders_three_part_block_and_did_you_mean():
    # "bord" is one edit from the real "board" command — the shared unknown_item_error builder
    # must suggest it. A bare string ("unknown command 'bord'") would not. The shared layer
    # gives the precise UNKNOWN_ITEM class (4), distinct from a plain bad-flag usage error (2).
    code, err = _capture(main, ["bord"])
    assert code == EXIT_UNKNOWN_ITEM
    assert "error:" in err  # the WHAT line of the shared block
    assert "fix:" in err  # the HOW-to-fix line
    assert "board" in err  # did-you-mean reached the real command name


def test_unknown_command_with_no_close_match_lists_known_commands():
    code, err = _capture(main, ["zzzzzzzz"])
    assert code == EXIT_UNKNOWN_ITEM
    assert "error:" in err and "fix:" in err
    # No close match -> the fix lists the real commands so the user can pick one.
    assert "ask" in err and "board" in err


# ── ask: empty question → EXIT_USAGE with a fix hint ─────────────────────────────────────


def test_ask_empty_question_renders_fix_hint():
    code, err = _capture(ask_cmd.run, ["   "])
    assert code == EXIT_USAGE
    assert "error:" in err and "fix:" in err
    # The fix tells the user HOW: pass a question.
    assert "question" in err.lower()


# ── ask: no seat answered → EXIT_NETWORK (was the ad-hoc 69) with a how-to-fix ───────────


def test_ask_no_seat_answered_is_network_class_with_fix(tmp_path, monkeypatch):
    # A REAL (non-offline) run with NO provider key and NO backend command: every seat is
    # unreachable (the key cascade resolves nothing), so 0 seats answer -> the NETWORK class
    # (was the ad-hoc 69). Deterministic + network-free: is_reachable only reads env + .env
    # files (no HTTP), so clearing the keys and running in an empty cwd guarantees 0 reachable.
    all_key_names = {n for names in PROVIDER_KEY_NAMES.values() for n in names}
    for name in all_key_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RESEARCH_BACKEND_CMD", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env in the empty tmp cwd -> the dotenv fallback finds none

    code, err = _capture(ask_cmd.run, ["what is the answer"])
    assert code == EXIT_NETWORK
    assert "error:" in err and "fix:" in err
    # The fix names a concrete remedy (a provider key, a backend, or --offline).
    low = err.lower()
    assert ("key" in low) or ("offline" in low) or ("backend" in low)


# ── ask: an engine ValueError maps to a diagnosed usage error (not a raw traceback) ──────


def test_ask_engine_value_error_is_usage_error(monkeypatch):
    # The engine can raise ValueError for an invalid argument value; the command layer must
    # turn it into the structured UsageError block, not let the traceback escape. Drive it by
    # forcing engine.run to raise ValueError (the empty-question guard short-circuits before the
    # engine, so this is the honest way to exercise the command's ValueError->UsageError map).
    from research_cli import engine as engine_mod

    def _boom(self, question):  # noqa: ARG001 — signature must match engine.run
        raise ValueError("invalid pool/seat value")

    monkeypatch.setattr(engine_mod.ResearchEngine, "run", _boom)
    code, err = _capture(ask_cmd.run, ["--offline", "a real question"])
    assert code == EXIT_USAGE
    assert "error:" in err and "fix:" in err
    assert "invalid pool/seat value" in err  # the original message reaches the WHAT line
    assert "Traceback" not in err


# ── ask: malformed manifest path → a diagnosed config (usage) error, not a traceback ─────


def test_ask_missing_manifest_renders_block_not_traceback(tmp_path):
    missing = tmp_path / "nope.yaml"
    code, err = _capture(ask_cmd.run, ["--manifest", str(missing), "q"])
    # A bad manifest is the usage/config class in the shared contract (was the ad-hoc 70).
    assert code == EXIT_USAGE
    assert "error:" in err and "fix:" in err
    assert "Traceback" not in err  # diagnosed, not a raw stack trace


# ── board: malformed manifest path → a diagnosed config (usage) error ────────────────────


def test_board_missing_manifest_renders_block(tmp_path):
    missing = tmp_path / "nope.yaml"
    code, err = _capture(board_cmd.run, ["--manifest", str(missing)])
    assert code == EXIT_USAGE
    assert "error:" in err and "fix:" in err
    assert "Traceback" not in err


# ── dispatcher: a command module that fails to load → EXIT_INTERNAL (a bug, not usage) ────


def test_cli_load_failure_is_internal(monkeypatch):
    # Force the lazy loader to blow up so the dispatcher's load-failure path is exercised.
    import research_cli.cli as cli_mod

    def _boom(_mod_name):
        raise RuntimeError("synthetic load failure")

    monkeypatch.setattr(cli_mod, "_load_run", _boom)
    code, err = _capture(main, ["board"])
    assert code == EXIT_INTERNAL
    assert "error:" in err  # rendered as the shared block, not a bare print
