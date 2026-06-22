"""Error-layer tests for the model-freshness checker (lib/checker/model_freshness.py).

These assert the checker renders failures through the shared `agenttools_errors`
layer (error-system v2: a 3-part WHAT / WHY / HOW-to-fix block + a stable per-class
exit code) instead of the old bare `print("error: ...")` + ad-hoc 1/2 codes.

Deliberately YAML-FREE: unlike `test_model_freshness.py` (which `importorskip`s yaml at
module top, so the whole file skips on a yaml-less CI runner), these tests inject the
failure via monkeypatch / a missing path, so the exit-code contract is actually exercised
in the dependency-free CI gate (`uv run --with pytest pytest tests/`, no extra deps).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The checker lives at lib/checker/; add lib/ so `from checker.model_freshness import ...`
# resolves the package the cron also runs (same shim the sibling test file uses).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lib"))

from agenttools_errors import EXIT_USAGE  # noqa: E402
from checker import model_freshness as mf  # noqa: E402


def test_errors_shim_re_exports_shared_layer():
    """`checker._errors` is the ONE import point — its names ARE the shared lib's objects."""
    from checker import _errors
    import agenttools_errors as shared

    # Re-export, don't re-implement: identity, not just equality.
    assert _errors.UsageError is shared.UsageError
    assert _errors.guard is shared.guard
    assert _errors.EXIT_USAGE is shared.EXIT_USAGE


def test_manifest_load_failure_renders_structured_block(monkeypatch, capsys):
    """A manifest that can't be loaded → EXIT_USAGE + a what/why/fix block, not a bare print."""
    def _boom(*_a, **_k):
        raise mf.ManifestError("invalid YAML in models.yaml: bad indent")

    monkeypatch.setattr(mf, "load_manifest", _boom)

    rc = mf.main([])

    assert rc == EXIT_USAGE  # the malformed-config / usage class (2), via the shared contract
    err = capsys.readouterr().err
    assert "error:" in err           # the WHAT line is always present
    assert "invalid YAML" in err     # the underlying ManifestError message is preserved
    assert "fix:" in err             # error-system v2: an actionable HOW-to-fix line


def test_manifest_load_failure_under_validate_also_structured(monkeypatch, capsys):
    """The --validate path uses the SAME structured load-failure error (not a different shape)."""
    def _boom(*_a, **_k):
        raise mf.ManifestError("cannot read manifest models.yaml: No such file")

    monkeypatch.setattr(mf, "load_manifest", _boom)

    rc = mf.main(["--validate"])

    assert rc == EXIT_USAGE
    err = capsys.readouterr().err
    assert "error:" in err
    assert "cannot read manifest" in err
    # The FIX must point at the actual manifest path + the re-validate command, not just exist —
    # so a broken f-string (a lost path) is caught, not silently green.
    fix_line = next(line for line in err.splitlines() if "fix:" in line)
    assert str(mf.MANIFEST_PATH) in fix_line
    assert "--validate" in fix_line


def test_validate_failure_renders_problems_and_fix(monkeypatch, capsys):
    """A manifest that LOADS but is INVALID → EXIT_USAGE, the problems listed, an actionable fix."""
    fake_manifest = object()
    monkeypatch.setattr(mf, "load_manifest", lambda *_a, **_k: fake_manifest)
    monkeypatch.setattr(
        mf,
        "validate_manifest",
        lambda _m: ["role `vision` -> `m1` which lacks the vision capability",
                    "role `code` -> unknown id `nope`"],
    )

    rc = mf.main(["--validate"])

    assert rc == EXIT_USAGE  # invalid config is the usage class (was the ad-hoc `1`)
    err = capsys.readouterr().err
    assert "error:" in err
    # both diagnosed problems must reach the user
    assert "lacks the vision capability" in err
    assert "unknown id `nope`" in err
    assert "fix:" in err              # a concrete next step, not just a dump
    # The two problems must stay READABLY SEPARATED, not run together. The shared render()
    # sanitizes embedded newlines away, so they're joined with a visible "; " delimiter — guard
    # against a regression that collapses them into one unreadable run-on (the `; ` is gone).
    why_line = next(line for line in err.splitlines() if "why:" in line)
    assert "; " in why_line
    assert "lacks the vision capability" in why_line and "unknown id `nope`" in why_line


def test_validate_success_still_exits_zero(monkeypatch, capsys):
    """The happy --validate path is unchanged: exit 0, a human OK line, nothing on stderr."""
    class _M:
        models = (1, 2, 3)
        roles = {"a": "b"}
        aliases = {}

    monkeypatch.setattr(mf, "load_manifest", lambda *_a, **_k: _M())
    monkeypatch.setattr(mf, "validate_manifest", lambda _m: [])

    rc = mf.main(["--validate"])

    assert rc == 0
    out = capsys.readouterr()
    assert "manifest OK" in out.out
    assert out.err == ""


def test_run_failure_renders_structured_block(monkeypatch, capsys):
    """A non-validate run whose manifest is invalid at run() time → the same structured error."""
    monkeypatch.setattr(mf, "load_manifest", lambda *_a, **_k: object())
    monkeypatch.setattr(mf, "validate_manifest", lambda _m: [])

    def _run_boom(*_a, **_k):
        raise mf.ManifestError("manifest invalid:\n  - role `x` -> unknown id")

    monkeypatch.setattr(mf, "run", _run_boom)

    rc = mf.main([])

    assert rc == EXIT_USAGE
    err = capsys.readouterr().err
    assert "error:" in err
    assert "unknown id" in err
    assert "fix:" in err


def test_unknown_flag_exits_usage_code_via_argparse():
    """An unknown flag exits 2 — argparse's own SystemExit, which the README claims == EXIT_USAGE.

    Locks that coupling with a test (argparse hard-codes 2; the shared `EXIT_USAGE` is also 2):
    if either side ever drifts, this asserts the contract the README documents.
    """
    import pytest

    assert EXIT_USAGE == 2  # the shared constant the README equates the argparse path to
    with pytest.raises(SystemExit) as exc:
        mf.main(["--no-such-flag"])
    assert exc.value.code == EXIT_USAGE
