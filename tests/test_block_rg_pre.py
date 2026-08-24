"""Tests for agent-hooks/block-rg-pre/block_rg_pre.py.

Covers:
  - True positives: `rg --pre CMD`, `rg --pre=CMD`, wrapped forms (sudo/timeout/env), shell
    chains (`;`/`&&`/`|`), `env -S` obfuscation.
  - The two false-positive classes the glob-only deny rules cannot express, now ALLOWED:
    `rg -- --pre .` (positional after end-of-options) and `rg -e --pre .` (`--pre` as the VALUE
    of `-e`/`--regexp`, not a flag).
  - `--pre-glob` alone is a distinct, non-dangerous flag — allowed.
  - Non-rg commands and rg invocations with no `--pre` are allowed.
  - Fail-closed: unbalanced quotes with a plausible `rg --pre` hint, malformed event.
  - External Telegram hatch: unset denies, blank/bare-flag value denies without asking,
    tg-ctl exit 0 allows, tg-ctl exit nonzero denies.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_block_rg_pre.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-rg-pre"
    / "block_rg_pre.py"
)
_spec = importlib.util.spec_from_file_location("block_rg_pre", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_HERMETIC_CWD = tempfile.mkdtemp(prefix="brp-hermetic-")
_HATCH_ENV_KEY = "RIG_HATCH_REQUEST_BLOCK_RG_PRE"


def _run(command: str, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    event: dict = {"args": {"command": command}, "cwd": _HERMETIC_CWD}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv(_HATCH_ENV_KEY, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_hook_is_directly_executable(tmp_path):
    event = {"cwd": str(tmp_path), "args": {"command": "rg --pre ./run.sh ."}}
    env = {k: v for k, v in os.environ.items() if k != _HATCH_ENV_KEY}
    proc = subprocess.run(
        [str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert proc.returncode == hook.BLOCK_EXIT_CODE, (proc.returncode, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["decision"] == "block"


# ── True positives: a real --pre flag — BLOCK ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "rg --pre ./run.sh .",
        "rg --pre=./run.sh .",
        "rg --pre 'sh -c \"id\"' .",
        "rg -i --pre foo.sh pattern .",
        "rg --pre foo.sh --pre-glob '*.gz' .",  # --pre AND --pre-glob together — still dangerous
    ],
)
def test_block_direct_pre_flag(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_explains_the_risk_and_hatch(monkeypatch):
    out, _err, _code = _run("rg --pre ./run.sh .", monkeypatch)
    msg = json.loads(out)["message"]
    assert "arbitrary" in msg.lower()
    assert "RIG_HATCH_REQUEST_BLOCK_RG_PRE" in msg


@pytest.mark.parametrize(
    "command",
    [
        "sudo rg --pre ./run.sh .",
        "timeout 5 rg --pre ./run.sh .",
        "env FOO=bar rg --pre ./run.sh .",
        "nice -n 5 rg --pre ./run.sh .",
    ],
)
def test_block_wrapped_forms(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi && rg --pre ./run.sh .",
        "echo hi; rg --pre ./run.sh .",
        "cat file | rg --pre ./run.sh .",
    ],
)
def test_block_in_shell_chain(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_env_split_string_obfuscation(monkeypatch):
    out, _err, code = _run("env -S 'rg --pre ./run.sh .'", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Regression: quoted redirect-shaped value swallowed --pre (P1, found in review) ─────────────

@pytest.mark.parametrize(
    "command",
    [
        "rg -e '>' --pre ./run.sh .",        # quoted redirect-shaped value glued to a short flag
        "rg --regexp '>' --pre ./run.sh .",  # same, long-flag form
        "rg '>' --pre ./run.sh .",           # a BARE quoted redirect-shaped positional (P1,
                                              # found in review TWICE — the first fix only
                                              # protected the value-flag-preceded shape above)
    ],
)
def test_block_pre_after_quoted_redirect_shaped_value(command, monkeypatch):
    """A quoted `>`/`<`-shaped token — whether it's a KNOWN rg value-flag's literal value, or a
    bare positional with nothing recognizable before it — is indistinguishable from a real
    redirect operator after shlex de-quoting. `_strip_redirects` must not swallow it (and the
    `--pre` after it) just because it looks like a redirect: either the PRECEDING token is a
    known value-flag (`_is_rg_value_flag_before_redirect`), or the FOLLOWING token (the
    redirect's would-be target) looks like a flag itself (`_looks_like_a_flag`) — a real file
    target never legitimately starts with a bare `-`."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_real_trailing_redirect_still_stripped_and_pre_still_found(monkeypatch):
    """Control case: an ACTUAL trailing redirect to a normal (non-flag-shaped) target is still
    recognized and stripped as before — this hook still finds --pre regardless."""
    out, _err, code = _run("rg pattern . --pre ./run.sh > out.txt", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_real_redirect_to_a_normal_target_with_no_pre(monkeypatch):
    """Control case: a real redirect to an ordinary (non-flag-shaped) target, with no --pre
    anywhere, is still allowed — the target-shape guard must not turn every redirect into a
    block."""
    out, _err, code = _run("rg pattern . > out.txt", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Regression: xargs-wrapped rg --pre (P1, found in review) ───────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "printf '%s\\n' needle README.md | xargs rg --pre ./run.sh",
        "printf '%s\\n' needle README.md | xargs -n2 rg --pre /usr/bin/true",
        "echo x | xargs env rg --pre ./run.sh",
        "echo x | xargs sudo rg --pre ./run.sh",
    ],
)
def test_block_xargs_wrapped_rg_pre(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_xargs_wrapped_rg_without_pre(monkeypatch):
    out, _err, code = _run("echo needle | xargs rg pattern", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_xargs_wrapping_non_rg_command(monkeypatch):
    out, _err, code = _run("echo hi | xargs echo", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── The two false-positive classes the glob belt can't express — ALLOW ─────────────────────

def test_allow_pre_after_end_of_options_marker(monkeypatch):
    """`rg -- --pre .` — everything after `--` is a literal positional, never a flag."""
    out, _err, code = _run("rg -- --pre .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pre_as_regexp_flag_value(monkeypatch):
    """`rg -e --pre .` searches for the literal text "--pre" via -e/--regexp — no --pre FLAG
    is present at all."""
    out, _err, code = _run("rg -e --pre .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pre_as_long_regexp_flag_value(monkeypatch):
    out, _err, code = _run("rg --regexp --pre .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pre_as_glued_short_flag_value(monkeypatch):
    """`-e--pre` glues "--pre" as -e's value in the same token."""
    out, _err, code = _run("rg -e--pre .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pre_glob_alone(monkeypatch):
    """--pre-glob is a distinct, non-dangerous flag — never conflated with --pre."""
    out, _err, code = _run("rg --pre-glob '*.gz' pattern .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pre_glob_glued_equals_form(monkeypatch):
    """The `--pre-glob=VALUE` glued form must be skipped the same as the separate-token form."""
    out, _err, code = _run("rg --pre-glob='*.gz' pattern .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_short_cluster_bool_then_regexp_value(monkeypatch):
    """`-ie --pre .` = `-i` (boolean) then `-e`'s value is the literal text "--pre" — allow."""
    out, _err, code = _run("rg -ie --pre .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_path_invoked_rg(monkeypatch):
    """A path to the rg binary (not the bare name) is still recognized."""
    out, _err, code = _run("/opt/homebrew/bin/rg --pre ./run.sh .", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pre_after_a_consumed_value_flag(monkeypatch):
    """`rg -e foo --pre ./x .` — `-e` consumes `foo` as ITS value, then `--pre` is a REAL flag.
    The natural adversary of `test_allow_pre_as_regexp_flag_value`: verifies the value-skipper
    doesn't under- or over-consume tokens (found in review)."""
    out, _err, code = _run("rg -e foo --pre ./x .", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Regression: leading shell-noise cap must fail CLOSED, not silently truncate (P1, review) ───

def test_leading_shell_noise_overflow_blocks(monkeypatch):
    """`_strip_leading_shell_noise` previously stopped stripping at its 16-token cap and
    returned a segment still headed by a noise token (e.g. `"("`), which `_is_rg_executable`
    reads as "not rg" — an undetected ALLOW that also skipped the fail-closed
    `_plausible_rg_pre` backstop. 17 nested subshells must now raise -> fail closed -> block."""
    command = "( " * (hook._MAX_LEADING_SHELL_NOISE + 1) + "rg --pre ./evil.sh ." + " )" * (
        hook._MAX_LEADING_SHELL_NOISE + 1
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_leading_shell_noise_within_cap_still_allowed_without_pre(monkeypatch):
    """Control case: noise strictly within the cap, no `--pre` present, still allows normally —
    the fix must not turn every deeply-nested-but-innocent command into a block."""
    command = "( " * hook._MAX_LEADING_SHELL_NOISE + "rg pattern ." + " )" * hook._MAX_LEADING_SHELL_NOISE
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Regression: unresolved command substitution in an rg argument (P1, review) ─────────────────

@pytest.mark.parametrize(
    "command",
    [
        'rg "$(printf -- --pre)" ./run.sh .',
        "rg `printf -- --pre` ./run.sh .",
        'rg pattern "$(echo x)" .',  # substitution need not spell --pre; still unverifiable -> block
    ],
)
def test_block_unresolved_substitution_in_rg_args(command, monkeypatch):
    """shlex only removes QUOTES — it never evaluates a substitution — so `"$(printf -- --pre)"`
    tokenizes to the literal string `$(printf -- --pre)`, not to what the shell will actually
    pass to rg (`--pre`) once it runs. Any unresolved `$(...)`/backtick token in an rg segment's
    own argv must fail closed rather than trust a literal-token comparison that can't see it."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_dollar_sign_with_no_substitution_syntax(monkeypatch):
    """Control case: a bare `$` (no `(` following, no backtick) is not substitution syntax and
    must not trip the new check."""
    out, _err, code = _run("rg 'price: $5' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Regression: inline RIPGREP_CONFIG_PATH can inject --pre from an unread file (P1, review) ───

@pytest.mark.parametrize(
    "command",
    [
        "RIPGREP_CONFIG_PATH=/tmp/x.rc rg pattern .",
        "env RIPGREP_CONFIG_PATH=/tmp/x.rc rg pattern .",
    ],
)
def test_block_inline_ripgrep_config_path(command, monkeypatch):
    """`RIPGREP_CONFIG_PATH` points rg at a config file that can carry its OWN `--pre` flag,
    invisible to any argv-level guard. This hook does not open and scan that file, so an rg
    invocation carrying the var INLINE (visible here, unlike an ambient/session-wide value)
    fails closed rather than trusting the unread file."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_ripgrep_config_path_on_non_rg_command(monkeypatch):
    """Control case: the var only matters when it actually precedes an rg invocation."""
    out, _err, code = _run("RIPGREP_CONFIG_PATH=/tmp/x.rc echo hi", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "export RIPGREP_CONFIG_PATH=/tmp/x.rc; rg pattern .",
        "declare RIPGREP_CONFIG_PATH=/tmp/x.rc; rg pattern .",
    ],
)
def test_block_ripgrep_config_path_exported_in_earlier_segment(command, monkeypatch):
    """P1 (found in review): an export in an EARLIER segment of the SAME command string must
    still reach a LATER rg invocation, not just a segment carrying the var inline."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_export_ripgrep_config_path_with_no_later_rg(monkeypatch):
    """Control case: an export with no rg invocation anywhere in the command is harmless."""
    out, _err, code = _run("export RIPGREP_CONFIG_PATH=/tmp/x.rc; echo hi", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Regression: an EXPLICIT empty --pre value is ripgrep's own no-op, not dangerous (P2, review) ─

@pytest.mark.parametrize(
    "command",
    [
        "rg --pre '' .",
        "rg --pre= .",
    ],
)
def test_allow_explicit_empty_pre_value(command, monkeypatch):
    """ripgrep documents an empty preprocessor command as disabling preprocessing — harmless,
    not the dangerous flag. Over-blocking it was a real (safe-direction) false positive."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_pre_with_missing_value_at_end_of_argv(monkeypatch):
    """Control case: a bare `--pre` with NOTHING after it (not an explicit empty string) is
    still dangerous — must not be confused with the explicit-empty-value no-op above."""
    out, _err, code = _run("rg --pre", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Ordinary usage — ALLOW ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "rg pattern .",
        "rg -i -n pattern src/",
        "rg -e pattern -e other .",
        "rg -t py --max-count 5 pattern .",
        "rg -A 3 -B 2 pattern .",
        "echo not rg at all",
        "",
    ],
)
def test_allow_ordinary_usage(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_unrelated_command_mentioning_pre_in_a_string(monkeypatch):
    out, _err, code = _run("echo 'please --pre-approve this'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Fail-closed paths ────────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_with_plausible_rg_pre_hint_blocks(monkeypatch):
    out, _err, code = _run("rg --pre 'unclosed .", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_command_allowed(monkeypatch):
    out, _err, code = _run("echo won't fail", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_wrapper_chain_overflow_blocks(monkeypatch):
    chain = "sudo " * (hook._MAX_WRAPPER_NESTING + 1) + "rg --pre ./run.sh ."
    out, _err, code = _run(chain, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_malformed_event_blocks(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", err_buf)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out_buf.getvalue()) == "block"


def test_missing_command_field_allows(monkeypatch):
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_missing_args_key_entirely_allows(monkeypatch):
    """The previous `""`-command test exercises the empty-string fallback, not a genuinely
    absent `args` key — this covers the `event.get("args") or {}` / `event.get("command")`
    fallback chain in `main()` when the event has no `args` at all (test nit, found in review)."""
    event: dict = {"cwd": _HERMETIC_CWD}
    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", err_buf)
    code = hook.main()
    assert code == 0
    assert _decision(out_buf.getvalue()) == "allow"


# ── External Telegram hatch escalation ──────────────────────────────────────────────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_hatch_unset_denies(monkeypatch):
    out, _err, code = _run("rg --pre ./run.sh .", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_hatch_blank_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("rg --pre ./run.sh .", monkeypatch, {_HATCH_ENV_KEY: "   "})
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert not (tmp_path / "asked").exists()


def test_hatch_bare_flag_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    _out, _err, code = _run("rg --pre ./run.sh .", monkeypatch, {_HATCH_ENV_KEY: "1"})
    assert code == hook.BLOCK_EXIT_CODE
    assert not (tmp_path / "asked").exists()


def test_hatch_exit0_allows(monkeypatch, tmp_path):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\nprintf 'approved by Telegram tap\\n'\nexit 0\n",
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "rg --pre ./decompress.sh archive.gz",
        monkeypatch,
        {_HATCH_ENV_KEY: "Searching inside a zstd archive, decompressor is trusted."},
    )
    assert code == 0
    assert _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_exit_nonzero_denies(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "rg --pre ./run.sh .",
        monkeypatch,
        {_HATCH_ENV_KEY: "Need a one-off exception."},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert "hatch escalation denied" in json.loads(out)["message"].lower()
