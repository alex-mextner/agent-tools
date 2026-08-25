"""Tests for the model-freshness checker (lib/checker/model_freshness.py).

Hermetic: provider endpoints are mocked via the injected `pollers` hook, `gh` via the
injected `gh_available` hook — no network, no real `gh`. The real manifest
(lib/contracts/models.yaml) is loaded for the happy-path/validation tests; synthetic
manifests (tmp_path) cover the malformed/invalid cases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The checker lives at lib/checker/; tests run from the repo root. Add lib/ to the path so
# `from checker.model_freshness import ...` resolves the package the cron also runs.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lib"))

from checker import model_freshness as mf  # noqa: E402

yaml = pytest.importorskip("yaml")

MANIFEST = _REPO_ROOT / "lib" / "contracts" / "models.yaml"
SCHEMA = _REPO_ROOT / "lib" / "contracts" / "models.schema.json"


# ── manifest load + validate (against the REAL shipped manifest) ────────────────────────
def test_real_manifest_loads_and_validates():
    m = mf.load_manifest(MANIFEST)
    assert m.version == 1
    assert m.models, "manifest must ship at least one model"
    assert mf.validate_manifest(m) == [], "shipped manifest must be valid"


def test_vision_role_resolves_to_a_vision_capable_model():
    """#3681: the `vision` role MUST point at a vision-capable entry."""
    m = mf.load_manifest(MANIFEST)
    target = m.roles.get("vision")
    assert target, "manifest must define a `vision` role"
    entry = m.entry(target)
    assert entry is not None
    assert "vision" in entry.capabilities


def test_kimi_code_has_no_vision_kimi_turbo_has_vision():
    """The two Kimi models are the canonical vision-flag example from #3681."""
    m = mf.load_manifest(MANIFEST)
    code = m.entry("moonshotai/Kimi-K2.7-Code")
    turbo = m.entry("kimi-k2p6-turbo")
    assert code is not None and turbo is not None
    assert "vision" not in code.capabilities, "Kimi-K2.7-Code is code-only, no vision"
    assert "vision" in turbo.capabilities, "kimi-k2p6-turbo has vision"


def test_kimi_code_k3_pin_on_real_manifest():
    """The k3 pin: kimi-code:latest resolves to it, and it really is vision-capable."""
    m = mf.load_manifest(MANIFEST)
    assert m.aliases["kimi-code:latest"] == "k3"
    entry = m.entry("k3")
    assert entry is not None and entry.provider == "kimi-code"
    assert "vision" in entry.capabilities  # omp catalog: images yes
    # Kimi docs: set the context-window field to 1048576 for k3's full up-to-1M context.
    assert entry.context == 1048576


def test_fallback_chain_ends_with_omp_k3():
    """The resilience order's LAST step is the omp+k3 daily-driver harness."""
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    chain = data["fallback_chain"]
    assert chain[-1] == {"harness": "omp", "model": "k3", "notation": "omp:k3"}


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_validate_flags_vision_role_to_non_vision_model(tmp_path: Path):
    data = {
        "version": 1,
        "models": [
            {"id": "code-only", "provider": "commandcode", "capabilities": ["code"]},
        ],
        "roles": {"vision": "code-only"},
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("violates #3681" in p for p in problems)


def test_validate_flags_role_pointing_at_unknown_id(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "real", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"architect": "does-not-exist"},
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("not a concrete model id" in p for p in problems)


def test_validate_flags_provider_latest_mismatch(tmp_path: Path):
    data = {
        "version": 1,
        "models": [
            {"id": "a", "provider": "openai", "capabilities": ["code"]},
            {"id": "b", "provider": "zai", "capabilities": ["code"]},
        ],
        "aliases": {"openai:latest": "b"},  # b is a zai model — mismatch
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("openai:latest" in p and "zai" in p for p in problems)


def test_validate_flags_unknown_capability(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "x", "provider": "openai", "capabilities": ["telepathy"]}],
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    problems = mf.validate_manifest(m)
    assert any("unknown capabilities" in p for p in problems)


def test_load_manifest_rejects_empty_models(tmp_path: Path):
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "models": []}), encoding="utf-8")
    with pytest.raises(mf.ManifestError):
        mf.load_manifest(p)


def test_load_manifest_context_bool_not_coerced_to_int(tmp_path: Path):
    """`context: true` must NOT silently become 1 (isinstance(True, int) is True)."""
    data = {"version": 1, "models": [
        {"id": "x", "provider": "openai", "capabilities": ["code"], "context": True},
    ]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    assert m.entry("x").context is None  # the bool is rejected, not coerced


# ── version comparison ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("gpt-5.6", "gpt-5.5", True),
        ("gpt-5.5", "gpt-5.5", False),
        ("gpt-5.4", "gpt-5.5", False),
        ("glm-5.3", "glm-5.2", True),
        ("claude-opus-4-9", "claude-opus-4-8", True),
        # different family is never "newer" — guards against cross-family proposals
        ("gemini-3.0", "gpt-5.5", False),
        ("claude-fable-6", "claude-opus-4-8", False),
        # DATED / build-suffixed snapshots the real endpoints return (the bug review caught):
        # a newer-version dated snapshot IS a bump for the bare pin…
        ("claude-opus-4-9-20260201", "claude-opus-4-8", True),
        ("gemini-2.5-flash-002", "gemini-2.4-flash", True),
        ("gpt-5.6-2026-01-01", "gpt-5.5", True),
        # …but a re-dated SAME version is NOT newer (no proposal churn on republish)…
        ("claude-opus-4-8-20260201", "claude-opus-4-8", False),
        ("gemini-2.5-flash-002", "gemini-2.5-flash", False),
        ("gpt-5.5-preview", "gpt-5.5", False),
        # …and an OLDER dated snapshot is never newer.
        ("claude-opus-4-7-20260201", "claude-opus-4-8", False),
        # kimi-code k3: a next-major IS a bump…
        ("k4", "k3", True),
        # …but k3-256k is a CONTEXT VARIANT of the same generation (`-256k` is not a
        # stripped snapshot suffix, so it lands in a different family) — never proposed.
        ("k3-256k", "k3", False),
        # …and the endpoint's task aliases are a different family entirely.
        ("kimi-for-coding", "k3", False),
        ("kimi-for-coding-highspeed", "k3", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert mf.is_newer(candidate, current) is expected


def test_strip_snapshot_suffix():
    assert mf.strip_snapshot_suffix("claude-opus-4-8-20260101") == "claude-opus-4-8"
    assert mf.strip_snapshot_suffix("gemini-2.5-flash-002") == "gemini-2.5-flash"
    assert mf.strip_snapshot_suffix("gpt-5.5-2026-01-01") == "gpt-5.5"
    assert mf.strip_snapshot_suffix("gpt-5.6-preview") == "gpt-5.6"
    assert mf.strip_snapshot_suffix("gpt-5.5") == "gpt-5.5"  # no suffix → unchanged


def test_model_family_strips_version():
    assert mf.model_family("gpt-5.5") == mf.model_family("gpt-5.6")
    assert mf.model_family("gpt-5.5") != mf.model_family("gemini-2.5-flash")
    # a dated snapshot shares the family of its bare pin (the whole point)
    assert mf.model_family("claude-opus-4-8-20260101") == mf.model_family("claude-opus-4-9")
    # a different segment count is a DIFFERENT family (next-major not auto-proposed)
    assert mf.model_family("claude-opus-4-8") != mf.model_family("claude-opus-5")


def test_compute_proposals_picks_newest_dated_snapshot(tmp_path: Path):
    """The endpoint returns dated snapshots; the proposal must pick the newest real version."""
    data = {"version": 1, "models": [
        {"id": "claude-opus-4-8", "provider": "anthropic", "capabilities": ["vision", "code"]},
    ]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"anthropic": mf.PollResult("anthropic", True, model_ids=[
        "claude-opus-4-8-20260101",   # same version, re-dated → not a bump
        "claude-opus-4-9-20260201",   # newer → the bump
        "claude-opus-4-7-20251201",   # older
    ])}
    proposals = mf.compute_proposals(m, polls)
    assert len(proposals) == 1
    assert proposals[0].new_id == "claude-opus-4-9-20260201"


# ── proposal computation (mocked endpoints) ─────────────────────────────────────────────
def _fake_poller(provider: str, ids: list[str]):
    def _poll(timeout: float):
        return mf.PollResult(provider, True, model_ids=ids)
    return _poll


def _skip_poller(provider: str, reason: str = "no key"):
    def _poll(timeout: float):
        return mf.PollResult(provider, False, skipped=reason)
    return _poll


def test_compute_proposals_finds_newer(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["vision", "code"]}],
    }
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", True, model_ids=["gpt-5.4", "gpt-5.6", "gpt-5.5"])}
    proposals = mf.compute_proposals(m, polls)
    assert len(proposals) == 1
    assert proposals[0].new_id == "gpt-5.6"
    assert proposals[0].current_id == "gpt-5.5"
    # capabilities are copied from the current pin (unverified)
    assert proposals[0].inferred_capabilities == ("vision", "code")


def test_compute_proposals_none_when_current(tmp_path: Path):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", True, model_ids=["gpt-5.5", "gpt-4.1"])}
    assert mf.compute_proposals(m, polls) == []


def test_compute_proposals_skips_unpolled_provider(tmp_path: Path):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    m = mf.load_manifest(_write_manifest(tmp_path, data))
    polls = {"openai": mf.PollResult("openai", False, skipped="no key")}
    assert mf.compute_proposals(m, polls) == []


# ── run(): report fallback when gh is absent ────────────────────────────────────────────
def test_run_writes_report_when_no_gh(tmp_path: Path, monkeypatch):
    data = {
        "version": 1,
        "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}],
    }
    manifest = _write_manifest(tmp_path, data)
    reports = tmp_path / "reports"
    monkeypatch.setattr(mf, "REPORTS_DIR", reports)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: False,
    )
    assert result.used_gh is False
    assert result.report_path is not None and result.report_path.is_file()
    body = result.report_path.read_text()
    assert "gpt-5.5" in body and "gpt-5.6" in body
    assert len(result.proposals) == 1


def test_run_report_records_skipped_provider(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "glm-5.2", "provider": "zai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    result = mf.run(
        manifest_path=manifest,
        pollers={"zai": _skip_poller("zai", "no ZAI_API_KEY")},
        gh_available=lambda: False,
    )
    body = result.report_path.read_text()
    assert "no ZAI_API_KEY" in body
    assert result.proposals == []
    # the report must NOT claim "all current" when a provider was skipped (incomplete coverage)
    assert "INCOMPLETE" in body
    assert "Every polled provider pin is current" not in body


def test_run_report_all_current_only_when_all_polled(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "REPORTS_DIR", tmp_path / "reports")
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.5", "gpt-4.1"])},  # polled, current
        gh_available=lambda: False,
    )
    body = result.report_path.read_text()
    assert "Every polled provider pin is current" in body


def test_run_dry_run_no_gh_writes_nothing(tmp_path: Path, monkeypatch):
    """--dry-run must NEVER write — including the report path when gh is absent (the bug)."""
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    reports = tmp_path / "reports"
    monkeypatch.setattr(mf, "REPORTS_DIR", reports)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: False,
        dry_run=True,
    )
    assert result.report_path is None
    assert not reports.exists(), "dry-run must not create the reports dir / a report file"
    assert any("dry-run" in a and "would write" in a for a in result.actions)
    assert len(result.proposals) == 1  # still computed


def test_run_invalid_manifest_raises(tmp_path: Path):
    data = {
        "version": 1,
        "models": [{"id": "code-only", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"vision": "code-only"},
    }
    manifest = _write_manifest(tmp_path, data)
    with pytest.raises(mf.ManifestError):
        mf.run(manifest_path=manifest, pollers={}, gh_available=lambda: False)


# ── gh path: idempotency + dry-run, with subprocess mocked ──────────────────────────────
def test_run_gh_dry_run_no_duplicate(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)

    # an OPEN PR already exists for this bump → propose_via_gh must skip (idempotent)
    monkeypatch.setattr(mf, "_open_pr_exists", lambda proposal: True)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: True,
        dry_run=True,
    )
    assert result.used_gh is True
    assert any("already exists" in a for a in result.actions)


def test_run_gh_dry_run_would_open(tmp_path: Path, monkeypatch):
    data = {"version": 1, "models": [{"id": "gpt-5.5", "provider": "openai", "capabilities": ["code"]}]}
    manifest = _write_manifest(tmp_path, data)
    monkeypatch.setattr(mf, "_open_pr_exists", lambda proposal: False)
    result = mf.run(
        manifest_path=manifest,
        pollers={"openai": _fake_poller("openai", ["gpt-5.6"])},
        gh_available=lambda: True,
        dry_run=True,
    )
    assert any("would open PR" in a for a in result.actions)


def test_proposal_slug_is_stable_and_safe():
    p = mf.Proposal("commandcode", "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.8-Code", ("code",))
    slug = p.slug
    assert "/" not in slug
    assert slug == mf.Proposal("commandcode", "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.8-Code", ("code",)).slug


# ── manifest rewriting (the surgical PR diff) ───────────────────────────────────────────
def test_rewrite_manifest_text_swaps_only_the_id():
    text = "models:\n  - id: gpt-5.5\n    provider: openai\n  - id: glm-5.2\n    provider: zai\n"
    out = mf.rewrite_manifest_text(text, "gpt-5.5", "gpt-5.6")
    assert "id: gpt-5.6" in out
    assert "id: glm-5.2" in out  # untouched
    assert "gpt-5.5" not in out


def test_rewrite_manifest_text_handles_quotes():
    text = 'models:\n  - id: "gpt-5.5"\n    provider: openai\n'
    out = mf.rewrite_manifest_text(text, "gpt-5.5", "gpt-5.6")
    assert 'id: "gpt-5.6"' in out


def test_rewrite_manifest_text_noop_when_absent():
    text = "models:\n  - id: gpt-5.5\n"
    assert mf.rewrite_manifest_text(text, "not-here", "x") == text


def test_rewrite_manifest_text_new_id_with_backslash_not_interpreted():
    """`new_id` from an endpoint must be inserted VERBATIM (no re.sub escape interpretation)."""
    text = "models:\n  - id: gpt-5.5\n"
    # an id containing a backslash + a group-ref-looking sequence — a replacement STRING would
    # try to expand \g<1>/\1 and corrupt the output; the callable inserts it literally.
    weird = r"weird\g<1>\1-id"
    out = mf.rewrite_manifest_text(text, "gpt-5.5", weird)
    assert weird in out
    assert "gpt-5.5" not in out


# ── key harvesting ──────────────────────────────────────────────────────────────────────
def test_resolve_key_prefers_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert mf.resolve_key(("OPENAI_API_KEY",)) == "sk-env"


def test_resolve_key_reads_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('OPENAI_API_KEY="sk-fromfile"\n', encoding="utf-8")
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(env_file))
    assert mf.resolve_key(("OPENAI_API_KEY",)) == "sk-fromfile"


def test_resolve_key_none_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NOPE_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    assert mf.resolve_key(("NOPE_API_KEY",)) is None


# ── endpoint parsers ────────────────────────────────────────────────────────────────────
def test_ids_from_openai_list():
    data = {"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}]}
    assert mf._ids_from_openai_list(data) == ["a", "b"]


def test_ids_from_gemini_list():
    data = {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-3.0"}]}
    assert mf._ids_from_gemini_list(data) == ["gemini-2.5-flash", "gemini-3.0"]


# ── kimi-code poller ────────────────────────────────────────────────────────────────────
def test_poll_kimi_code_skips_without_key(tmp_path: Path, monkeypatch):
    """No KIMI_API_KEY (env or .env fallback) and no omp OAuth login → a clean skip, never a crash."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: None)
    result = mf._poll_kimi_code(1.0)
    assert result.provider == "kimi-code"
    assert result.ok is False
    assert "KIMI_API_KEY" in result.skipped
    assert result.error == ""


def test_poll_kimi_code_uses_omp_oauth_token_without_api_key(tmp_path: Path, monkeypatch):
    """No KIMI_API_KEY but an omp kimi-code OAuth login → the stored access token polls."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_FRESHNESS_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append((url, headers)) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [
        ("https://api.kimi.com/coding/v1/models", {"Authorization": "Bearer oauth-tok"})
    ]


def test_poll_kimi_code_api_key_beats_omp_oauth(tmp_path: Path, monkeypatch):
    """Precedence: an explicit KIMI_API_KEY wins over the omp OAuth credential."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append(headers) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [{"Authorization": "Bearer sk-kimi"}]


def test_omp_kimi_code_oauth_token_reads_agent_db(tmp_path: Path, monkeypatch):
    """The resolver reads provider=kimi-code/credential_type=oauth from omp's agent.db."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    db = sqlite3.connect(db_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "  tok-123  "}),),
    )
    # a disabled / wrong-provider / non-oauth row must NOT be picked
    db.execute(
        "INSERT INTO auth_credentials VALUES (2, 'kimi-code', 'oauth', ?, 'revoked', 20)",
        (json.dumps({"access": "tok-disabled"}),),
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (3, 'openai', 'oauth', ?, NULL, 30)",
        (json.dumps({"access": "tok-other"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    assert mf._omp_kimi_code_oauth_token() == "tok-123"


def test_omp_kimi_code_oauth_token_missing_db_is_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    assert mf._omp_kimi_code_oauth_token() is None


def test_omp_kimi_code_oauth_token_malformed_row_is_none(tmp_path: Path, monkeypatch):
    """not-json, valid JSON of the wrong shape, and a null access must ALL resolve to None
    (never raise, never produce a `Bearer None`)."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    for payload in ("not-json", "[]", "null", json.dumps({"access": None})):
        db_file = db_dir / "agent.db"
        db_file.unlink(missing_ok=True)
        db = sqlite3.connect(db_file)
        db.execute(
            "CREATE TABLE auth_credentials"
            " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
            " disabled_cause TEXT, updated_at INTEGER)"
        )
        db.execute(
            "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
            (payload,),
        )
        db.commit()
        db.close()
        assert mf._omp_kimi_code_oauth_token() is None, payload


def test_omp_token_cli_output_is_used(monkeypatch):
    """Primary path: `omp token kimi-code` stdout is the token (refresh-aware resolver)."""
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        assert cmd == ["omp", "token", "kimi-code"]
        return _sp.CompletedProcess(cmd, 0, stdout="fresh-tok\n", stderr="")

    monkeypatch.setattr(mf.subprocess, "run", fake_run)
    assert mf._omp_token_cli() == "fresh-tok"


def test_omp_token_cli_failure_falls_back_to_db(tmp_path: Path, monkeypatch):
    """omp CLI missing/failing → the read-only agent.db peek still resolves the token."""
    import sqlite3

    db_dir = tmp_path / ".omp" / "agent"
    db_dir.mkdir(parents=True)
    db = sqlite3.connect(db_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "db-tok"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path))

    def boom(cmd, **kwargs):
        raise FileNotFoundError("omp")

    monkeypatch.setattr(mf.subprocess, "run", boom)
    assert mf._omp_kimi_code_oauth_token() == "db-tok"


def test_cross_origin_redirect_strips_authorization():
    """A 30x to a different origin must NOT carry the caller's Authorization header."""
    import urllib.request

    handler = mf._NoCrossOriginAuthRedirect()
    req = urllib.request.Request(
        "https://api.kimi.com/coding/v1/models",
        headers={"Authorization": "Bearer secret"},
    )
    new = handler.redirect_request(
        req, None, 302, "Found", {}, "http://evil.example.test/models"
    )
    assert new is not None
    assert "Authorization" not in new.headers
    assert "Authorization" not in new.unredirected_hdrs


def test_same_origin_redirect_keeps_authorization():
    import urllib.request

    handler = mf._NoCrossOriginAuthRedirect()
    req = urllib.request.Request(
        "https://api.kimi.com/coding/v1/models",
        headers={"Authorization": "Bearer secret"},
    )
    new = handler.redirect_request(
        req, None, 301, "Moved", {}, "https://api.kimi.com/coding/v2/models"
    )
    assert new is not None
    assert new.headers.get("Authorization") == "Bearer secret"


def test_omp_kimi_code_oauth_token_honors_agent_dir_env(tmp_path: Path, monkeypatch):
    """omp can relocate its agent dir (OMP_CODING_AGENT_DIR / PI_CODING_AGENT_DIR) — the
    resolver must follow the same env vars instead of only looking in ~/.omp/agent."""
    import sqlite3

    agent_dir = tmp_path / "custom-agent"
    agent_dir.mkdir()
    db = sqlite3.connect(agent_dir / "agent.db")
    db.execute(
        "CREATE TABLE auth_credentials"
        " (id INTEGER, provider TEXT, credential_type TEXT, data TEXT,"
        " disabled_cause TEXT, updated_at INTEGER)"
    )
    db.execute(
        "INSERT INTO auth_credentials VALUES (1, 'kimi-code', 'oauth', ?, NULL, 10)",
        (json.dumps({"access": "tok-relocated"}),),
    )
    db.commit()
    db.close()
    monkeypatch.setenv("HOME", str(tmp_path / "no-omp-here"))
    monkeypatch.setattr(mf, "_omp_token_cli", lambda: None)  # force the db fallback path
    monkeypatch.setenv("OMP_CODING_AGENT_DIR", str(agent_dir))
    assert mf._omp_kimi_code_oauth_token() == "tok-relocated"
    monkeypatch.delenv("OMP_CODING_AGENT_DIR")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    assert mf._omp_kimi_code_oauth_token() == "tok-relocated"


def test_poll_kimi_code_oauth_token_never_leaves_canonical_endpoint(monkeypatch):
    """A custom KIMI_CODE_BASE_URL must NOT receive the harvested omp OAuth token —
    endpoint overrides require an explicitly supplied KIMI_API_KEY."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(mf, "_omp_kimi_code_oauth_token", lambda: "oauth-tok")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "http://evil.example.test/v1")
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is False
    assert "KIMI_API_KEY" in result.skipped
    assert calls == [], "no request may be made with the OAuth token to a custom base"


def test_poll_kimi_code_custom_base_ok_with_explicit_key(monkeypatch):
    """The same custom base IS allowed when the caller supplied their own KIMI_API_KEY."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://kimi.example.test/v1")
    calls = []
    monkeypatch.setattr(
        mf,
        "_http_get_json",
        lambda url, headers, timeout: calls.append((url, headers)) or {"data": []},
    )
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert calls == [
        ("https://kimi.example.test/v1/models", {"Authorization": "Bearer sk-kimi"})
    ]


def test_poll_kimi_code_parses_openai_list(monkeypatch):
    """With a key, GET <base>/models and parse the OpenAI-compatible id list."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.delenv("KIMI_CODE_BASE_URL", raising=False)
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers))
        return {"data": [{"id": "k3"}, {"id": "k3-256k"}, {"id": "kimi-for-coding"}]}

    monkeypatch.setattr(mf, "_http_get_json", fake_get)
    result = mf._poll_kimi_code(1.0)
    assert result.ok is True
    assert result.model_ids == ["k3", "k3-256k", "kimi-for-coding"]
    assert calls == [
        ("https://api.kimi.com/coding/v1/models", {"Authorization": "Bearer sk-kimi"})
    ]


def test_poll_kimi_code_honors_base_url_override(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://kimi.example.test/v1/")
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    mf._poll_kimi_code(1.0)
    assert calls == ["https://kimi.example.test/v1/models"]  # trailing slash stripped


def test_poll_kimi_code_ignores_kimi_base_url(monkeypatch):
    """KIMI_BASE_URL is the direct-API moonshot var, NOT the coding-endpoint override —
    setting it must not redirect this poller (only KIMI_CODE_BASE_URL does)."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.delenv("KIMI_CODE_BASE_URL", raising=False)
    calls = []
    monkeypatch.setattr(
        mf, "_http_get_json", lambda url, headers, timeout: calls.append(url) or {"data": []}
    )
    mf._poll_kimi_code(1.0)
    assert calls == ["https://api.kimi.com/coding/v1/models"]


def test_poll_kimi_code_http_error_is_error_not_crash(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")

    def boom(url, headers, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(mf, "_http_get_json", boom)
    result = mf._poll_kimi_code(1.0)
    assert result.ok is False
    assert "connection refused" in result.error


def test_kimi_code_poller_registered():
    assert mf.POLLERS["kimi-code"] is mf._poll_kimi_code


# ── schema conformance: the manifest validates against models.schema.json ───────────────
def test_manifest_matches_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)


def test_schema_enum_matches_known_capabilities():
    """The checker's KNOWN_CAPABILITIES must match the schema's capability enum (no drift)."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    enum = schema["$defs"]["capability"]["enum"]
    assert set(enum) == set(mf.KNOWN_CAPABILITIES)


# ── main() exit codes + structured errors (#89: adopt agenttools_errors) ────────────────
# main() is the cron entrypoint; a calling script branches on its exit code, so the code per
# failure class is a contract. These cover the boundary the unit tests above never touched.
import agenttools_errors as errs  # noqa: E402  (lib/ is on sys.path; sibling of checker)


def _invalid_manifest(tmp_path: Path) -> Path:
    """A manifest that loads but violates a cross-reference invariant (vision→non-vision)."""
    data = {
        "version": 1,
        "models": [{"id": "code-only", "provider": "openai", "capabilities": ["code"]}],
        "roles": {"vision": "code-only"},
    }
    return _write_manifest(tmp_path, data)


def test_main_validate_invalid_manifest_exits_usage_not_internal(tmp_path, monkeypatch, capsys):
    """A manifest that violates an invariant is a CONFIG error → exit 2 (EXIT_USAGE/CONFIG),
    NOT 1 (EXIT_INTERNAL is reserved for unexpected bugs/tracebacks)."""
    manifest = _invalid_manifest(tmp_path)
    monkeypatch.setattr(mf, "load_manifest", _load_factory(manifest))

    code = mf.main(["--validate"])
    # An invariant violation raises ConfigError → EXIT_CONFIG. (EXIT_USAGE == EXIT_CONFIG == 2,
    # but assert against the constant the code actually raises so this can't silently lie if
    # agenttools_errors ever splits the two.)
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err  # structured 3-part block, not a bare line


def test_main_malformed_manifest_exits_usage(tmp_path, monkeypatch, capsys):
    """An unparseable / non-mapping manifest → structured config error, exit 2."""
    bad = tmp_path / "models.yaml"
    bad.write_text("just a string, not a mapping\n", encoding="utf-8")
    monkeypatch.setattr(mf, "load_manifest", _load_factory(bad))

    code = mf.main([])  # live run path; load_manifest raises before any network
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err


def test_main_missing_pyyaml_exits_missing_dep(monkeypatch, capsys):
    """PyYAML absent is a missing-DEPENDENCY failure → exit 127 + an install hint,
    distinct from a malformed-config (2)."""
    def _raise_missing_dep(*a, **k):
        # MissingYamlError is exactly what load_manifest() raises when `import yaml` fails.
        raise mf.MissingYamlError(
            "PyYAML is required to read the manifest but is not installed. "
            "Install it: `python3 -m pip install --user pyyaml`."
        )
    monkeypatch.setattr(mf, "load_manifest", _raise_missing_dep)

    code = mf.main(["--validate"])
    assert code == errs.EXIT_MISSING_DEP == 127
    out = capsys.readouterr()
    blob = out.err + out.out
    assert "install" in blob.lower()


def test_main_validate_ok_exits_zero(monkeypatch, capsys):
    """The happy path stays byte-compatible: a valid manifest → exit 0, human summary."""
    monkeypatch.setattr(mf, "load_manifest", _load_factory(MANIFEST))
    code = mf.main(["--validate"])
    assert code == errs.EXIT_OK == 0
    assert "manifest OK" in capsys.readouterr().out


def test_main_run_path_manifest_error_exits_config(monkeypatch, capsys):
    """The SECOND ManifestError mapping — a fault surfacing from run() (not the initial
    load_manifest) — must also render structured + exit 2. run() reloads the manifest, so a
    ManifestError from there is the live-run analogue of the validate-path config error."""
    monkeypatch.setattr(mf, "load_manifest", _load_factory(MANIFEST))  # initial load is fine

    def _boom(*a, **k):
        raise mf.ManifestError("manifest invalid:\n  - vision role points at a non-vision model")
    monkeypatch.setattr(mf, "run", _boom)

    code = mf.main([])  # live-run path (no --validate)
    assert code == errs.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert "why:" in err and "fix:" in err


def test_main_unexpected_bug_propagates_not_swallowed(monkeypatch):
    """Contract (README): an UNEXPECTED exception is a bug → guard() must let it propagate
    (traceback + exit 1), NOT diagnose it as a config error. A regression where guard starts
    swallowing arbitrary exceptions would otherwise pass unnoticed."""
    def _bug(*a, **k):
        raise RuntimeError("something genuinely unexpected broke")
    monkeypatch.setattr(mf, "load_manifest", _bug)

    with pytest.raises(RuntimeError, match="genuinely unexpected"):
        mf.main(["--validate"])


def _load_factory(path: Path):
    """Return a load_manifest replacement that ALWAYS reads `path` (main() calls it with no
    arg, so the default-path plumbing can't be redirected otherwise). Delegates to the real
    parser by importing the module's function object captured before patching."""
    real = _REAL_LOAD_MANIFEST

    def _load_manifest(p: Path = path) -> "mf.Manifest":  # noqa: F821
        return real(path)

    return _load_manifest


# Capture the genuine parser once, before any test monkeypatches mf.load_manifest.
_REAL_LOAD_MANIFEST = mf.load_manifest
