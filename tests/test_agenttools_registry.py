"""Tests for agenttools_registry — the shared trust kernel + trust-gated registry.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_registry.py -q
    # or, if agenttools-registry is installed:  python -m pytest tests/ -q

Every test is HOME-isolated (a throwaway ``tmp_path``), deterministic, and does NO network
and NO sleeps. The trust kernel is pure (digests of bytes + a JSON store), so tests pin its
contract by writing entry files into tmp and asserting the decision.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_registry as reg  # noqa: E402
from agenttools_registry import (  # noqa: E402
    Entry,
    Registry,
    TrustDecision,
    TrustState,
    TrustStore,
    audit_decision,
    entry_invocation_digest,
    invocation_digest,
    is_quarantined,
    is_runnable,
    load_python_entry,
    sha256_file,
    sha256_str,
    trust_state,
)


# --- helpers ----------------------------------------------------------------------------
def _write(path: Path, content: str = "print('hi')\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _entry(tmp: Path, name: str = "m", content: str = "x = 1\n", **kw) -> Entry:
    cmd = _write(tmp / f"{name}.py", content)
    return Entry(name=name, cmd=cmd, **kw)


def _empty_store() -> TrustStore:
    return TrustStore()


# --- digests ----------------------------------------------------------------------------
def test_sha256_file_hashes_bytes(tmp_path: Path):
    f = _write(tmp_path / "a.py", "abc")
    import hashlib

    assert sha256_file(f) == hashlib.sha256(b"abc").hexdigest()


def test_sha256_file_missing_returns_none(tmp_path: Path):
    assert sha256_file(tmp_path / "nope.py") is None


def test_sha256_str_is_stable():
    assert sha256_str("hello") == sha256_str("hello")
    assert sha256_str("hello") != sha256_str("hellp")


def test_invocation_digest_matches_tg_layout():
    # tg's invocationDigest: [cmd, ...args, "\0timeout=<n>"].join("\0"), then sha256.
    # Our invocation_digest hashes that same string (plus an optional \0extra= trailer
    # which is ABSENT when extra is empty — so it equals the tg digest for the no-extra
    # case).
    expected = sha256_str("\0".join(["/bin/x", "a", "b", "\0timeout=5000"]))
    assert invocation_digest("/bin/x", ["a", "b"], 5000) == expected


def test_invocation_digest_unset_vs_explicit_timeout_differ():
    assert invocation_digest("/bin/x") != invocation_digest("/bin/x", timeout_ms=0)


def test_invocation_digest_args_swap_changes_it():
    assert invocation_digest("/bin/x", ["a"]) != invocation_digest("/bin/x", ["b"])


def test_invocation_digest_extra_changes_it():
    base = invocation_digest("/bin/x", ["a"])
    assert invocation_digest("/bin/x", ["a"], extra=["selection"]) != base
    # empty extra collapses to the no-extra digest (the \0extra= marker is only appended
    # when extra is non-empty), so a tag widening from [] -> [tag] re-quarantines.
    assert invocation_digest("/bin/x", ["a"], extra=[]) == base


def test_extra_digest_distinguishes_token_boundaries():
    # ["a", "bc"] must differ from ["ab", "c"] — the NUL join makes tokens unambiguous.
    assert invocation_digest("/bin/x", extra=["a", "bc"]) != invocation_digest("/bin/x", extra=["ab", "c"])


def test_entry_invocation_digest_uses_entry_fields(tmp_path: Path):
    e = Entry(name="m", cmd=tmp_path / "m.py", args=("--x",), timeout_ms=100, extra_digest=("tag",))
    assert entry_invocation_digest(e) == invocation_digest(e.cmd, ("--x",), 100, ("tag",))


# --- trust-by-default (guard off) -------------------------------------------------------
def test_guard_off_is_trusted_default(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=False)
    assert d.state is TrustState.TRUSTED_DEFAULT
    assert d.runnable is True
    assert d.cmd_sha256 == sha256_file(e.cmd)
    assert "trust-by-default" in d.reason


def test_guard_off_does_not_consult_store(tmp_path: Path):
    # Even a never-seen entry is trusted with the guard off.
    e = _entry(tmp_path, name="unknown")
    d = trust_state(e, _empty_store(), guard=False)
    assert d.runnable is True


# --- missing cmd ------------------------------------------------------------------------
def test_missing_cmd_is_untrusted_regardless_of_guard(tmp_path: Path):
    e = Entry(name="ghost", cmd=tmp_path / "does-not-exist.py")
    for guard in (False, True):
        d = trust_state(e, _empty_store(), guard=guard)
        assert d.state is TrustState.UNTRUSTED_MISSING_CMD
        assert d.runnable is False
        assert d.cmd_sha256 is None


# --- guarded TOFU: new / changed / trusted ---------------------------------------------
def test_guard_on_new_entry_is_quarantined_new(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=True)
    assert d.state is TrustState.QUARANTINED_NEW
    assert d.runnable is False


def test_guard_on_auto_bypasses_pins(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=True, auto=True)
    assert d.state is TrustState.AUTO
    assert d.runnable is True


def test_auto_has_no_effect_when_guard_off(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=False, auto=True)
    # guard off short-circuits before auto is even considered → trusted-default, not auto.
    assert d.state is TrustState.TRUSTED_DEFAULT


def test_guard_on_matching_pin_is_trusted(tmp_path: Path):
    e = _entry(tmp_path)
    store = _empty_store()
    pinned = trust_state(e, store, guard=True)  # quarantined-new, but carries fresh digests
    store.pin(pinned)
    d = trust_state(e, store, guard=True)
    assert d.state is TrustState.TRUSTED
    assert d.runnable is True


def test_guard_on_changed_cmd_bytes_requarantines(tmp_path: Path):
    e = _entry(tmp_path, content="v1\n")
    store = _empty_store()
    store.pin(trust_state(e, store, guard=True))
    # edit the entry file's bytes
    e.cmd.write_text("v2 — tampered\n", encoding="utf-8")
    d = trust_state(e, store, guard=True)
    assert d.state is TrustState.QUARANTINED_CHANGED
    assert "cmd changed" in d.reason


def test_guard_on_changed_invocation_requarantines(tmp_path: Path):
    # Pin with one set of args, then change args (cmd bytes unchanged) → re-quarantine.
    cmd = _write(tmp_path / "i.py", "same bytes\n")
    e1 = Entry(name="i", cmd=cmd, args=("--a",))
    store = _empty_store()
    store.pin(trust_state(e1, store, guard=True))
    e2 = Entry(name="i", cmd=cmd, args=("--b",))  # repointed via args
    d = trust_state(e2, store, guard=True)
    assert d.state is TrustState.QUARANTINED_CHANGED
    assert "invocation changed" in d.reason


def test_guard_on_widened_extra_digest_requarantines(tmp_path: Path):
    # review's activates_on case: a manifest-only tag widening keeps cmd bytes but must
    # re-trust. Modeled via extra_digest.
    cmd = _write(tmp_path / "t.py", "bytes\n")
    e1 = Entry(name="t", cmd=cmd, extra_digest=("selection",))
    store = _empty_store()
    store.pin(trust_state(e1, store, guard=True))
    e2 = Entry(name="t", cmd=cmd, extra_digest=("selection", "layout"))  # widened
    d = trust_state(e2, store, guard=True)
    assert d.state is TrustState.QUARANTINED_CHANGED


def test_pin_without_invocation_sha_is_stale(tmp_path: Path):
    # A forward-compat read of an old pin (no invocation_sha256) must re-trust.
    e = _entry(tmp_path)
    from agenttools_registry import TrustPin

    store = TrustStore(pins={e.name: TrustPin(cmd_sha256=sha256_file(e.cmd), invocation_sha256=None)})
    d = trust_state(e, store, guard=True)
    assert d.state is TrustState.QUARANTINED_CHANGED


# --- state classifiers ------------------------------------------------------------------
def test_is_runnable_and_is_quarantined_partition_states():
    runnable = {TrustState.TRUSTED_DEFAULT, TrustState.TRUSTED, TrustState.AUTO}
    for s in TrustState:
        assert is_runnable(s) == (s in runnable)
        assert is_quarantined(s) == (s not in runnable)
        assert is_runnable(s) != is_quarantined(s)  # exactly one is true


def test_trust_state_str_is_bare_value():
    assert str(TrustState.TRUSTED_DEFAULT) == "trusted-default"
    assert f"{TrustState.QUARANTINED_NEW}" == "quarantined-new"


# --- TrustStore round-trip + perms ------------------------------------------------------
def test_store_save_and_load_round_trip(tmp_path: Path):
    e = _entry(tmp_path)
    store = TrustStore(tmp_path / "trust.json")
    store.pin(trust_state(e, store, guard=True), meta={"on_error": "closed"})
    store.save()
    reloaded = TrustStore.load(tmp_path / "trust.json")
    pin = reloaded.get(e.name)
    assert pin is not None
    assert pin.cmd_sha256 == sha256_file(e.cmd)
    assert pin.invocation_sha256 == entry_invocation_digest(e)
    assert pin.meta == {"on_error": "closed"}


def test_store_file_is_0600(tmp_path: Path):
    e = _entry(tmp_path)
    p = tmp_path / "sub" / "trust.json"  # parent dir created on save
    store = TrustStore(p)
    store.pin(trust_state(e, store, guard=True))
    store.save()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_store_load_missing_file_is_empty(tmp_path: Path):
    store = TrustStore.load(tmp_path / "absent.json")
    assert len(store) == 0
    assert store.get("anything") is None


def test_store_load_garbage_is_empty(tmp_path: Path):
    p = _write(tmp_path / "garbage.json", "{not json")
    assert len(TrustStore.load(p)) == 0


def test_store_load_skips_malformed_rows_keeps_good(tmp_path: Path):
    p = tmp_path / "mixed.json"
    p.write_text(
        json.dumps(
            {
                "good": {"cmd_sha256": "abc", "invocation_sha256": "def"},
                "no-sha": {"invocation_sha256": "x"},  # missing cmd_sha256 → skipped
                "not-a-dict": 42,  # skipped
            }
        ),
        encoding="utf-8",
    )
    store = TrustStore.load(p)
    assert store.get("good") is not None
    assert store.get("no-sha") is None
    assert store.get("not-a-dict") is None


def test_store_pin_missing_cmd_raises(tmp_path: Path):
    d = TrustDecision(
        name="x", state=TrustState.UNTRUSTED_MISSING_CMD, reason="", cmd_sha256=None,
        invocation_sha256="i", runnable=False,
    )
    with pytest.raises(ValueError):
        _empty_store().pin(d)


def test_store_remove(tmp_path: Path):
    e = _entry(tmp_path)
    store = _empty_store()
    store.pin(trust_state(e, store, guard=True))
    assert store.remove(e.name) is True
    assert store.remove(e.name) is False  # idempotent


def test_store_save_without_path_raises():
    with pytest.raises(ValueError):
        TrustStore().save()


# --- audit ------------------------------------------------------------------------------
def test_audit_appends_one_json_line_per_call(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=False)
    ap = tmp_path / "cache" / "audit.jsonl"  # parent created on write
    audit_decision(ap, d, outcome="loaded", duration_ms=1.234)
    audit_decision(ap, d, outcome="loaded")
    lines = [ln for ln in ap.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["name"] == e.name
    assert row["trust_state"] == "trusted-default"
    assert row["outcome"] == "loaded"
    assert row["cmd_sha256"] == sha256_file(e.cmd)
    assert row["duration_ms"] == 1.23  # rounded to 2dp


def test_audit_none_path_is_noop(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=False)
    audit_decision(None, d, outcome="loaded")  # must not raise, must not create anything


def test_audit_extra_fields_merged(tmp_path: Path):
    e = _entry(tmp_path)
    d = trust_state(e, _empty_store(), guard=False)
    ap = tmp_path / "audit.jsonl"
    audit_decision(ap, d, outcome="block", extra={"point": "pre-send"})
    row = json.loads(ap.read_text(encoding="utf-8").strip())
    assert row["point"] == "pre-send"


# --- Registry facade --------------------------------------------------------------------
def test_registry_gate_splits_runnable_and_quarantined_guard_off(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    b = _entry(tmp_path, name="b")
    ghost = Entry(name="ghost", cmd=tmp_path / "nope.py")
    r = Registry(guard=False)
    result = r.gate([a, b, ghost])
    runnable_names = {ge.entry.name for ge in result.runnable}
    quarantined_names = {ge.entry.name for ge in result.quarantined}
    assert runnable_names == {"a", "b"}  # trust-by-default
    assert quarantined_names == {"ghost"}  # missing cmd → inert


def test_registry_gate_guard_on_quarantines_new(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    r = Registry(guard=True)
    result = r.gate([a])
    assert result.runnable == []
    assert {ge.entry.name for ge in result.quarantined} == {"a"}


def test_registry_trust_then_gate_runs(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    store_path = tmp_path / "trust.json"
    r = Registry(store_path=store_path, guard=True)
    r.trust(a)  # pin + save
    # a fresh registry loading the saved store now trusts it
    r2 = Registry(store_path=store_path, guard=True)
    result = r2.gate([a])
    assert {ge.entry.name for ge in result.runnable} == {"a"}
    assert store_path.exists()


def test_registry_trust_writes_pin_and_audits(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    store_path = tmp_path / "trust.json"
    audit_path = tmp_path / "audit.jsonl"
    r = Registry(store_path=store_path, audit_path=audit_path, guard=True)
    d = r.trust(a, meta={"on_error": "open"})
    # trust() returns the PRE-pin decision (it pins from the fresh digests it just
    # computed), so for a never-seen entry that decision is quarantined-new — the pin is
    # what makes the NEXT gate trusted (asserted below).
    assert d.state is TrustState.QUARANTINED_NEW
    assert d.cmd_sha256 == sha256_file(a.cmd)
    saved = TrustStore.load(store_path)
    assert saved.get("a").meta == {"on_error": "open"}
    # the pin makes a subsequent guarded gate trust the entry
    assert r.decide(a).state is TrustState.TRUSTED
    rows = [json.loads(x) for x in audit_path.read_text().splitlines() if x.strip()]
    assert any(row["outcome"] == "trust-pinned" for row in rows)


def test_registry_gate_audits_each_decision(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    ghost = Entry(name="ghost", cmd=tmp_path / "nope.py")
    audit_path = tmp_path / "audit.jsonl"
    r = Registry(audit_path=audit_path, guard=False)
    r.gate([a, ghost])
    rows = [json.loads(x) for x in audit_path.read_text().splitlines() if x.strip()]
    by_name = {row["name"]: row for row in rows}
    assert by_name["a"]["outcome"] == "runnable"
    assert by_name["ghost"]["outcome"] == "absent"


def test_registry_gate_custom_outcome_labeler(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    audit_path = tmp_path / "audit.jsonl"
    r = Registry(audit_path=audit_path, guard=False)
    r.gate([a], audit_outcome=lambda ge: "custom-loaded")
    row = json.loads(audit_path.read_text().strip())
    assert row["outcome"] == "custom-loaded"


def test_registry_decide_does_not_run_or_audit(tmp_path: Path):
    a = _entry(tmp_path, name="a")
    audit_path = tmp_path / "audit.jsonl"
    r = Registry(audit_path=audit_path, guard=False)
    d = r.decide(a)
    assert d.runnable is True
    assert not audit_path.exists()  # decide() does not audit


# --- load_python_entry ------------------------------------------------------------------
def test_load_python_entry_top_level_module(tmp_path: Path):
    cmd = _write(tmp_path / "mod.py", "MODULE = {'kind': 'top'}\n")
    obj = load_python_entry(cmd)
    assert obj == {"kind": "top"}


def test_load_python_entry_factory(tmp_path: Path):
    cmd = _write(tmp_path / "fac.py", "def get_module():\n    return {'kind': 'factory'}\n")
    obj = load_python_entry(cmd)
    assert obj == {"kind": "factory"}


def test_load_python_entry_class(tmp_path: Path):
    cmd = _write(tmp_path / "cls.py", "class Module:\n    kind = 'class'\n")
    obj = load_python_entry(cmd)
    assert getattr(obj, "kind", None) == "class"


def test_load_python_entry_broken_file_returns_none(tmp_path: Path):
    cmd = _write(tmp_path / "boom.py", "raise RuntimeError('explode at import')\n")
    assert load_python_entry(cmd) is None


def test_load_python_entry_no_exported_object_returns_none(tmp_path: Path):
    cmd = _write(tmp_path / "empty.py", "x = 1\n")  # no MODULE/factory/Module
    assert load_python_entry(cmd) is None


def test_load_python_entry_missing_file_returns_none(tmp_path: Path):
    assert load_python_entry(tmp_path / "nope.py") is None


# --- module surface ---------------------------------------------------------------------
def test_public_api_is_complete():
    for sym in reg.__all__:
        assert hasattr(reg, sym), sym
    assert reg.__version__


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
