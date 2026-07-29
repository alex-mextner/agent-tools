"""Tests for the model-error-fallback agent-hook + its pure state machine.

Two layers:
  * the PURE logic (`fallback_chain.py`) — the error-count / threshold / swap / re-dispatch /
    recover / exhaustion state machine, the transient-vs-normal classifier, manifest-chain
    building, and the snapshot round-trip. Deterministic, no I/O.
  * the HOOK shell (`model_error_fallback.py`) — the agents-hooks/v1 stdin/stdout protocol,
    state persistence across turns (a tmp state dir), and the manifest read. No network, no
    sleeps; the only filesystem touch is pytest's tmp_path.

Run from the repo root::

    uv run --with pytest --with pyyaml python -m pytest tests/test_model_error_fallback.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK_DIR = (
    Path(__file__).resolve().parents[1] / "agent-hooks" / "model-error-fallback"
)


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, _HOOK_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses introspecting `__module__` (frozen dataclasses with
    # forward-referenced annotations) can find the module in sys.modules.
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


fc = _load("fallback_chain", "fallback_chain.py")
hook = _load("model_error_fallback", "model_error_fallback.py")


# ── the classifier ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Error 429 Too Many Requests",
        "the server is temporarily limiting requests",
        "model overloaded, retry later",
        "HTTP 503 Service Unavailable",
        "got a 500 from the backend",
        "504 Gateway Timeout",
        "HTTP 520",  # Cloudflare gateway error (transient)
        "523 Origin Is Unreachable",  # Cloudflare gateway (transient)
        "HTTP 529",  # Anthropic overloaded code, bare number with no 'overloaded' word
        "rate limit exceeded",
        "you have exhausted your quota",
        "request throttled by the provider",
        "at capacity right now",
        "the service is over capacity",
    ],
)
def test_transient_errors_classified_as_transient(text):
    assert fc.is_transient_model_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "AssertionError: expected 3 got 4",
        "test_foo failed",
        "SyntaxError: invalid syntax",
        "I cannot help with that request",  # a refusal is not transient
        "I don't have the capacity to help with that",  # 'capacity' alone must NOT match
        "I have no capacity to help with that",  # 'no capacity' is a refusal, not an outage
        "I don't have great capacity for that",  # 'at capacity' must not match inside a word
        "this exceeds my capacity for nuance",
        "file not found",
        "",
        "compilation failed: undefined symbol",
        "got a 501 Not Implemented",  # 501 is a real client/impl error, not transient
    ],
)
def test_normal_failures_not_transient(text):
    assert fc.is_transient_model_error(text) is False


# ── default chain shape ──────────────────────────────────────────────────────────────────


def test_default_chain_is_the_documented_cross_harness_order():
    labels = [s.label for s in fc.DEFAULT_CHAIN]
    assert labels == ["claude:fable", "claude:opus", "oc:GLM-5.2", "codex:gpt5.5", "omp:k3"]
    # fable->opus is same-harness (a swap); claude->oc, oc->codex, codex->omp cross the boundary.
    assert fc.DEFAULT_CHAIN[0].harness == fc.DEFAULT_CHAIN[1].harness == "claude"
    assert fc.DEFAULT_CHAIN[2].harness == "oc"
    assert fc.DEFAULT_CHAIN[3].harness == "codex"
    assert fc.DEFAULT_CHAIN[4].harness == "omp"


# ── count / threshold ────────────────────────────────────────────────────────────────────


def test_below_threshold_does_not_switch():
    st = fc.FallbackState(threshold=3)
    d1 = st.observe(error=True, detail="429 rate limit")
    d2 = st.observe(error=True, detail="429 rate limit")
    assert d1.switched is False and d2.switched is False
    assert st.active.label == "claude:fable"
    assert st.consecutive_errors == 2


def test_nth_consecutive_transient_error_switches_to_next_step():
    st = fc.FallbackState(threshold=3)
    for _ in range(2):
        assert st.observe(error=True, detail="overloaded").switched is False
    d = st.observe(error=True, detail="overloaded")  # the 3rd
    assert d.switched is True
    assert d.kind == "fall"
    assert d.crosses_harness is False  # fable -> opus is same harness (a swap)
    assert d.from_step.label == "claude:fable"
    assert d.to_step.label == "claude:opus"
    assert st.active.label == "claude:opus"
    assert st.consecutive_errors == 0  # reset after a switch


def test_threshold_one_switches_on_first_error():
    st = fc.FallbackState(threshold=1)
    d = st.observe(error=True, detail="503")
    assert d.switched is True
    assert st.active.label == "claude:opus"


def test_threshold_must_be_positive():
    with pytest.raises(fc.FallbackError):
        fc.FallbackState(threshold=0)


# ── cross-harness re-dispatch vs in-harness swap ─────────────────────────────────────────


def test_crossing_the_harness_boundary_is_flagged():
    st = fc.FallbackState(threshold=1)
    swap = st.observe(error=True, detail="429")  # fable -> opus (same harness)
    assert swap.kind == "fall" and swap.crosses_harness is False
    redis = st.observe(error=True, detail="429")  # opus(claude) -> oc:GLM-5.2
    assert redis.kind == "fall"
    assert redis.crosses_harness is True
    assert redis.to_step.harness == "oc"
    last = st.observe(error=True, detail="429")  # oc -> codex (cross-harness)
    assert last.kind == "fall"
    assert last.crosses_harness is True
    assert last.to_step.label == "codex:gpt5.5"


# ── non-transient failures never burn the chain ──────────────────────────────────────────


def test_normal_failure_is_ignored_for_the_chain():
    st = fc.FallbackState(threshold=2)
    st.observe(error=True, detail="429")  # 1 transient
    d = st.observe(error=True, detail="AssertionError: nope")  # a real bug, not a throttle
    assert d.switched is False
    assert d.kind == "none"
    # The normal failure neither advanced nor reset the count: still 1 transient pending.
    assert st.consecutive_errors == 1
    assert st.active.label == "claude:fable"


def test_error_with_no_detail_is_treated_as_transient():
    # The host already classified it as a model error before passing error=True.
    st = fc.FallbackState(threshold=1)
    d = st.observe(error=True, detail="")
    assert d.switched is True


def test_non_transient_error_does_not_break_the_consecutive_accumulation():
    # A non-transient turn between two 429s is a no-op for the chain: it neither resets nor
    # advances, so the transient count survives it and the second 429 still reaches the
    # threshold. Confirm the count reflects only the transient turns.
    st = fc.FallbackState(threshold=2)
    st.observe(error=True, detail="429")  # count 1
    st.observe(error=True, detail="AssertionError")  # ignored, count stays 1
    assert st.consecutive_errors == 1
    d = st.observe(error=True, detail="429")  # count 2 -> threshold
    assert d.switched is True
    assert st.active.label == "claude:opus"


def test_history_is_bounded():
    # A long flapping session (throttle -> recover -> throttle …) must not grow history
    # without bound — it is capped at MAX_HISTORY.
    st = fc.FallbackState(threshold=1)
    for _ in range(200):
        st.observe(error=True, detail="429")  # fall (or exhausted dedup at the bottom)
        st.observe(error=False)  # recover
    assert len(st.history) <= st.MAX_HISTORY


# ── recovery / return-to-top ─────────────────────────────────────────────────────────────


def test_success_resets_count_at_the_top():
    st = fc.FallbackState(threshold=3)
    st.observe(error=True, detail="429")
    st.observe(error=True, detail="429")
    d = st.observe(error=False)  # a clean turn
    assert d.switched is False
    assert st.consecutive_errors == 0
    assert st.active.label == "claude:fable"


def test_success_after_falling_promotes_back_toward_the_top():
    st = fc.FallbackState(threshold=1)
    st.observe(error=True, detail="429")  # -> opus
    st.observe(error=True, detail="429")  # -> oc:GLM-5.2
    assert st.active.label == "oc:GLM-5.2"
    d = st.observe(error=False)  # recovered
    assert d.switched is True
    assert d.kind == "recover"
    assert st.active.label == "claude:opus"  # one step back toward the top
    st.observe(error=False)  # recovered again
    assert st.active.label == "claude:fable"  # back at the preferred model


def test_recovery_that_crosses_a_harness_is_flagged_as_crossing():
    # oc:GLM-5.2 -> claude:opus is a recovery that ALSO crosses the harness boundary, so the
    # host must re-dispatch back to the claude harness, not in-process-swap from oc. This is
    # the regression guard for the crosses_harness-only-on-fall bug.
    st = fc.FallbackState(threshold=1)
    st.observe(error=True, detail="429")  # fable -> opus
    st.observe(error=True, detail="429")  # opus -> oc:GLM-5.2 (now in the oc harness)
    d = st.observe(error=False)  # recover oc -> claude:opus
    assert d.kind == "recover"
    assert d.crosses_harness is True  # oc -> claude crosses, even though it's a recovery
    assert d.from_step.harness == "oc"
    assert d.to_step.harness == "claude"


def test_in_harness_recovery_does_not_cross():
    # claude:opus -> claude:fable is a recovery that stays in the claude harness.
    st = fc.FallbackState(threshold=1)
    st.observe(error=True, detail="429")  # fable -> opus
    d = st.observe(error=False)  # recover opus -> fable, same harness
    assert d.kind == "recover"
    assert d.crosses_harness is False


def test_recovery_then_throttle_again_walks_back_down():
    st = fc.FallbackState(threshold=1)
    st.observe(error=True, detail="429")  # opus
    st.observe(error=False)  # back to fable
    assert st.active.label == "claude:fable"
    st.observe(error=True, detail="429")  # opus again
    assert st.active.label == "claude:opus"


# ── exhaustion ───────────────────────────────────────────────────────────────────────────


def test_chain_exhaustion_is_loud_and_does_not_wrap():
    st = fc.FallbackState(threshold=1)
    st.observe(error=True, detail="429")  # opus
    st.observe(error=True, detail="429")  # oc
    st.observe(error=True, detail="429")  # codex
    st.observe(error=True, detail="429")  # omp (last)
    assert st.at_last_resort is True
    d = st.observe(error=True, detail="429")  # nowhere left to fall
    assert d.switched is False
    assert d.exhausted is True  # machine-readable, not just a grep of the reason
    assert "EXHAUSTED" in d.reason
    assert st.active.label == "omp:k3"  # stays put, does not wrap to the top


def test_exhausted_is_false_before_the_last_resort():
    st = fc.FallbackState(threshold=1)
    d = st.observe(error=True, detail="429")  # fable -> opus, plenty of chain left
    assert d.exhausted is False


def test_consecutive_errors_is_capped_at_exhaustion():
    # A task stranded at the last resort must not grow consecutive_errors without bound — it
    # is capped at the threshold so the persisted int stays small.
    st = fc.FallbackState(threshold=2)
    for _ in range(20):
        st.observe(error=True, detail="429")
    assert st.at_last_resort is True
    assert st.consecutive_errors <= st.threshold


# ── manifest chain building ──────────────────────────────────────────────────────────────


def test_chain_from_manifest_steps_preserves_order():
    steps = [
        {"harness": "claude", "model": "fable", "notation": "claude:fable"},
        {"harness": "oc", "model": "GLM-5.2"},
    ]
    chain = fc.chain_from_manifest_steps(steps)
    assert [s.label for s in chain] == ["claude:fable", "oc:GLM-5.2"]
    # notation falls back to harness:model when absent.
    assert chain[1].label == "oc:GLM-5.2"


def test_manifest_notation_propagates_to_the_active_label():
    # An explicit `notation` distinct from `harness:model` must be the surfaced label, not a
    # recomputed harness:model — it's the human-facing name a host shows when the step is live.
    steps = [
        {"harness": "claude", "model": "fable", "notation": "Claude Fable (preferred)"},
        {"harness": "oc", "model": "GLM-5.2", "notation": "GLM via opencode"},
    ]
    chain = fc.chain_from_manifest_steps(steps)
    st = fc.FallbackState(chain=chain, threshold=1)
    d = st.observe(error=True, detail="429")  # fall to step 2
    assert d.active.label == "GLM via opencode"
    assert chain[0].label == "Claude Fable (preferred)"


def test_chain_from_manifest_rejects_empty_and_malformed_steps():
    with pytest.raises(fc.FallbackError):
        fc.chain_from_manifest_steps([])
    with pytest.raises(fc.FallbackError):
        fc.chain_from_manifest_steps([{"harness": "claude"}])  # no model
    with pytest.raises(fc.FallbackError):
        fc.chain_from_manifest_steps([{"model": "fable"}])  # no harness


def test_shipped_manifest_fallback_chain_matches_the_default():
    manifest = (
        Path(__file__).resolve().parents[1] / "lib" / "contracts" / "models.yaml"
    )
    yaml = pytest.importorskip("yaml")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    steps = raw.get("fallback_chain")
    assert steps, "models.yaml must define fallback_chain (the ONE shared chain definition)"
    chain = fc.chain_from_manifest_steps(steps)
    assert [s.label for s in chain] == [s.label for s in fc.DEFAULT_CHAIN]


def test_shipped_manifest_validates_against_the_schema():
    # The manifest (incl. the new fallback_chain + fallbackStep $def) must satisfy its own
    # JSON-Schema — additionalProperties:false at the top level means an un-declared
    # fallback_chain key would FAIL, so this guards the schema/manifest staying in lockstep.
    root = Path(__file__).resolve().parents[1] / "lib" / "contracts"
    yaml = pytest.importorskip("yaml")
    jsonschema = pytest.importorskip("jsonschema")
    data = yaml.safe_load((root / "models.yaml").read_text(encoding="utf-8"))
    schema = json.loads((root / "models.schema.json").read_text(encoding="utf-8"))
    # Top-level additionalProperties:false is what makes an un-declared fallback_chain FAIL,
    # so the schema had to grow the property — assert it explicitly here.
    assert schema.get("additionalProperties") is False
    jsonschema.validate(data, schema)  # raises on violation
    # And a malformed step (missing required `model`) must be REJECTED by the schema.
    bad = dict(data)
    bad["fallback_chain"] = [{"harness": "claude"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# ── snapshot round-trip ──────────────────────────────────────────────────────────────────


def test_snapshot_round_trips_position_and_count():
    st = fc.FallbackState(threshold=2)
    st.observe(error=True, detail="429")  # 1 pending
    st.observe(error=True, detail="429")  # -> opus
    st.observe(error=True, detail="429")  # 1 pending on opus
    snap = st.snapshot()
    assert snap["active"] == "claude:opus"
    restored = fc.state_from_snapshot(snap)
    assert restored.index == st.index
    assert restored.consecutive_errors == st.consecutive_errors
    assert restored.active.label == "claude:opus"


def test_negative_snapshot_index_fails_loud():
    # A negative index is genuine corruption — there is no sensible position to recover.
    with pytest.raises(fc.FallbackError):
        fc.state_from_snapshot({"index": -1, "threshold": 3})


def test_too_large_snapshot_index_clamps_not_raises():
    # The manifest chain shrank between turns: a saved index past the new end CLAMPS to the
    # last step (preserve position, don't reset to the top and re-burn the chain). This is
    # the reachable path the hook actually takes via state_from_snapshot(snap, chain).
    short_chain = fc.DEFAULT_CHAIN[:2]  # fable, opus
    st = fc.state_from_snapshot({"index": 3, "threshold": 1}, chain=short_chain)
    assert st.index == 1  # clamped from 3 to the last valid index
    assert st.active.label == "claude:opus"


def test_history_as_a_string_is_not_split_into_chars():
    # A str is technically a Sequence; it must NOT be read char-by-char as the history.
    st = fc.state_from_snapshot({"index": 0, "threshold": 3, "history": "fable->opus"})
    assert st.history == []


def test_garbage_consecutive_errors_degrades_softly_keeping_position():
    # A non-numeric consecutive_errors must NOT crash the load (which would reset to the top
    # and re-burn the chain) — it defaults to 0 while the valid index is preserved.
    st = fc.state_from_snapshot(
        {"index": 1, "threshold": 1, "consecutive_errors": "abc"}, chain=fc.DEFAULT_CHAIN
    )
    assert st.consecutive_errors == 0
    assert st.index == 1  # position kept


def test_max_history_is_a_class_constant_not_an_init_param():
    # ClassVar: MAX_HISTORY must not be a constructor parameter (so it can't be disabled
    # per-instance). Passing it as a kwarg is a TypeError.
    with pytest.raises(TypeError):
        fc.FallbackState(MAX_HISTORY=0)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad_snap", [42, "x", [1, 2, 3], None])
def test_non_mapping_snapshot_raises_fallback_error(bad_snap):
    # A state file holding a bare scalar/array must raise FallbackError (which the hook
    # catches and resets), not an unguarded AttributeError out of snap.get(...).
    with pytest.raises(fc.FallbackError):
        fc.state_from_snapshot(bad_snap)


@pytest.mark.parametrize("bad_index", [[1, 2], {"a": 1}])
def test_non_integer_index_raises_fallback_error(bad_index):
    # int([...]) / int({...}) raises TypeError; it must be normalised to FallbackError so it
    # joins the reset-to-top handling rather than crashing the fail-open hook.
    with pytest.raises(fc.FallbackError):
        fc.state_from_snapshot({"index": bad_index, "threshold": 3})


# ── chain loading from the manifest (with the offline fallback) ──────────────────────────


def test_load_chain_uses_the_manifest_when_present(monkeypatch):
    # With a readable manifest the hook reads its fallback_chain (the ONE shared definition).
    chain = hook._load_chain()
    assert [s.label for s in chain] == [s.label for s in fc.DEFAULT_CHAIN]


def test_load_chain_falls_back_to_default_when_manifest_unreadable(monkeypatch):
    # Make the manifest unreadable: _load_chain must NOT crash and must return the baked-in
    # DEFAULT_CHAIN, never an empty chain (an empty chain could never switch — the wedge).
    real_read_text = Path.read_text

    def boom(self, *a, **k):
        if self.name == "models.yaml":
            raise FileNotFoundError(str(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    chain = hook._load_chain()
    assert [s.label for s in chain] == [s.label for s in fc.DEFAULT_CHAIN]
    assert len(chain) >= 1  # never empty


def test_load_chain_falls_back_when_a_manifest_step_is_malformed(monkeypatch):
    # A manifest with a step missing 'model' makes chain_from_manifest_steps raise, which
    # _load_chain catches and degrades to DEFAULT_CHAIN — never an empty/partial chain.
    real_read_text = Path.read_text

    def bad_manifest(self, *a, **k):
        if self.name == "models.yaml":
            return "version: 1\nfallback_chain:\n  - {harness: claude}\n"  # no model
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", bad_manifest)
    chain = hook._load_chain()
    assert [s.label for s in chain] == [s.label for s in fc.DEFAULT_CHAIN]


# ── the hook protocol ────────────────────────────────────────────────────────────────────


def _run_hook(event, monkeypatch, state_dir, env=None):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("AGENTTOOLS_FALLBACK_STATE_DIR", str(state_dir))
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _event(**args):
    return {"hook_api": "agents-hooks/v1", "tool": "Stop", "point": "stop", "args": args}


def test_hook_always_allows_and_is_well_formed(tmp_path, monkeypatch):
    out, _, code = _run_hook(
        _event(task_id="t1", error=True, error_text="429 rate limit"),
        monkeypatch,
        tmp_path,
        env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    assert payload["hook_api"] == "agents-hooks/v1"
    assert out.endswith("\n")


def test_hook_switches_after_threshold_across_turns(tmp_path, monkeypatch):
    # Three transient-error turns of the SAME task; state persists across hook invocations.
    last = None
    for _ in range(3):
        out, _, _ = _run_hook(
            _event(task_id="task-A", error=True, error_text="overloaded"),
            monkeypatch,
            tmp_path,
            env={"AGENTTOOLS_FALLBACK_THRESHOLD": "3"},
        )
        last = json.loads(out)
    assert last["fallback"]["switched"] is True
    assert last["fallback"]["active"] == "claude:opus"
    assert last["fallback"]["kind"] == "fall"
    assert last["fallback"]["crosses_harness"] is False  # fable->opus stays in-harness
    # An in-harness fall is a plain model swap: no cross-harness target either way.
    assert "redispatch_to" not in last["fallback"]
    assert "prefer_next" not in last["fallback"]


def test_hook_emits_redispatch_target_on_cross_harness_switch(tmp_path, monkeypatch):
    # threshold 1: fable->opus->oc. The oc step crosses the harness boundary.
    out = None
    for _ in range(2):
        raw, _, _ = _run_hook(
            _event(task_id="task-B", error=True, error_text="429"),
            monkeypatch,
            tmp_path,
            env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
        )
        out = json.loads(raw)
    assert out["fallback"]["crosses_harness"] is True
    assert out["fallback"]["redispatch_to"] == {"harness": "oc", "model": "GLM-5.2"}


def test_hook_ignores_a_turn_with_no_outcome_signal(tmp_path, monkeypatch):
    out, _, code = _run_hook(_event(task_id="t2"), monkeypatch, tmp_path)
    payload = json.loads(out)
    assert code == 0 and payload["decision"] == "allow"
    assert "fallback" not in payload  # nothing to report

    # And it did not create state for a no-op turn.
    assert not (tmp_path / "t2.json").exists()


def test_hook_does_not_switch_on_a_normal_failure(tmp_path, monkeypatch):
    out, _, _ = _run_hook(
        _event(task_id="t3", error=True, error_text="AssertionError: boom"),
        monkeypatch,
        tmp_path,
        env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    payload = json.loads(out)
    assert payload["fallback"]["switched"] is False
    assert payload["fallback"]["active"] == "claude:fable"


def test_hook_recovers_on_explicit_error_false_without_success_flag(tmp_path, monkeypatch):
    # A clean turn can be signalled by error=False, not only success=True. After a fall it
    # must recover the same way.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="ef", error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(_event(task_id="ef", error=False), monkeypatch, tmp_path, env)
    fb = json.loads(raw)["fallback"]
    assert fb["kind"] == "recover"
    assert fb["active"] == "claude:fable"


def test_hook_removes_state_file_when_recovered_to_pristine_top(tmp_path, monkeypatch):
    # After a fall the state file exists; once recovery brings it back to the pristine top
    # (index 0, zero pending), the file is removed in that same turn.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="cl", error=True, error_text="429"), monkeypatch, tmp_path, env)
    assert (tmp_path / "cl.json").exists()  # fell to opus -> non-pristine -> persisted
    _run_hook(_event(task_id="cl", error=False), monkeypatch, tmp_path, env)  # recover to top
    assert not (tmp_path / "cl.json").exists()  # pristine again -> file removed


def test_hook_keys_state_by_session_id_when_no_task_id(tmp_path, monkeypatch):
    # The unit-of-work key is task_id, then event_id, then session_id. With only a stable
    # session_id, accumulation must persist across turns under that key.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "2"}
    _run_hook(_event(session_id="sess-1", error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(
        _event(session_id="sess-1", error=True, error_text="429"), monkeypatch, tmp_path, env
    )
    assert (tmp_path / "sess-1.json").exists()
    assert json.loads(raw)["fallback"]["switched"] is True  # 2nd error hits threshold 2


def test_hook_default_key_when_no_id_present(tmp_path, monkeypatch):
    # No task_id / event_id / session_id -> a single shared 'default' state file, and two
    # such turns accumulate against it (so an id-less workload still gets a fallback budget).
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "2"}
    _run_hook(_event(error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(_event(error=True, error_text="429"), monkeypatch, tmp_path, env)
    assert (tmp_path / "default.json").exists()
    assert json.loads(raw)["fallback"]["switched"] is True  # 2nd error hits threshold 2


def test_hook_fail_open_when_state_cannot_be_persisted(tmp_path, monkeypatch):
    # _save_state hitting an OSError must NOT break the turn (advisory, fail-open): the hook
    # still allows and still reports the switch decision for this turn.
    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    out, _, code = _run_hook(
        _event(task_id="sf", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    assert payload["fallback"]["switched"] is True


def test_hook_recovers_on_success(tmp_path, monkeypatch):
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="t4", error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(_event(task_id="t4", success=True), monkeypatch, tmp_path, env)
    payload = json.loads(raw)
    assert payload["fallback"]["kind"] == "recover"
    assert payload["fallback"]["active"] == "claude:fable"


def test_hook_recovery_across_harness_prefers_next_does_not_redispatch(tmp_path, monkeypatch):
    # Fall fable->opus->oc (cross-harness), then a success recovers oc->claude:opus, which
    # ALSO crosses the boundary. A recovery fires AFTER the work succeeded, so the hook must
    # NOT emit redispatch_to (that would re-run completed work) — it emits prefer_next instead.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    for _ in range(2):  # fable->opus->oc
        _run_hook(_event(task_id="rec", error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(_event(task_id="rec", success=True), monkeypatch, tmp_path, env)
    fb = json.loads(raw)["fallback"]
    assert fb["kind"] == "recover"
    assert fb["crosses_harness"] is True
    assert "redispatch_to" not in fb  # never re-run completed work
    assert fb["prefer_next"] == {"harness": "claude", "model": "opus"}


def test_hook_live_env_threshold_overrides_saved_one_mid_task(tmp_path, monkeypatch):
    # Turn 1 with threshold 5: one error, no switch (count=1 < 5), saved threshold=5.
    _run_hook(
        _event(task_id="thr", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "5"},
    )
    # Turn 2 the env LOWERS the threshold to 1; it must take effect immediately despite the
    # saved snapshot's threshold of 5 — the next error switches.
    raw, _, _ = _run_hook(
        _event(task_id="thr", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    fb = json.loads(raw)["fallback"]
    assert fb["switched"] is True
    assert fb["active"] == "claude:opus"


def test_hook_exhaustion_is_surfaced_and_does_not_wrap(tmp_path, monkeypatch):
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    last = None
    for _ in range(6):  # fall through the whole 5-step chain, then one extra error
        raw, _, _ = _run_hook(
            _event(task_id="ex", error=True, error_text="529 overloaded"),
            monkeypatch, tmp_path, env,
        )
        last = json.loads(raw)
    assert last["fallback"]["active"] == "omp:k3"  # stays at the last resort
    assert last["fallback"]["switched"] is False
    assert "EXHAUSTED" in last["fallback"]["reason"]


def test_hook_path_traversal_key_is_sanitized(tmp_path, monkeypatch):
    # A traversal-looking task id must resolve to a plain filename inside the state dir.
    _run_hook(
        _event(task_id="../../etc/passwd", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    # The state file is a single segment directly under the state dir, no traversal escaped.
    assert written[0].parent == tmp_path
    assert "/" not in written[0].name


def test_hook_reads_the_detail_key_as_error_channel(tmp_path, monkeypatch):
    # The error channel is `error_text` OR `detail`; a host using `detail` must work too.
    out, _, _ = _run_hook(
        _event(task_id="dt", error=True, detail="429 rate limit"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert json.loads(out)["fallback"]["switched"] is True


def test_hook_reads_the_model_error_key(tmp_path, monkeypatch):
    # A host may signal via `model_error` instead of `error`.
    out, _, _ = _run_hook(
        _event(task_id="me", model_error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert json.loads(out)["fallback"]["switched"] is True


def test_hook_infers_error_from_the_error_channel_without_a_flag(tmp_path, monkeypatch):
    # No explicit flag — a transient string in the ERROR channel (error_text/detail) is read
    # as an error signal.
    out, _, _ = _run_hook(
        _event(task_id="inf", error_text="HTTP 529 overloaded"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert json.loads(out)["fallback"]["switched"] is True


def test_explicit_error_flag_with_only_output_is_trusted_not_reclassified(tmp_path, monkeypatch):
    # Host sets error=True but gives no error_text — only `output`. The output must NOT be
    # classified: an explicit error flag is trusted (treated as transient), and incidental
    # keywords (or their absence) in the agent's normal output must not flip that.
    # (a) benign output, explicit error flag -> still counts as a (transient) error and switches
    out, _, _ = _run_hook(
        _event(task_id="ex1", error=True, output="here is the refactored function"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert json.loads(out)["fallback"]["switched"] is True
    # (b) output full of incidental keywords, NO error flag -> no inference, no switch
    out2, _, _ = _run_hook(
        _event(task_id="ex2", output="added rate limiting and a throttle, overloaded the API"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    p2 = json.loads(out2)
    assert "fallback" not in p2  # no signal -> no-op
    assert not (tmp_path / "ex2.json").exists()


def test_hook_does_not_infer_from_model_output_text(tmp_path, monkeypatch):
    # The agent's NORMAL response text (output/message) routinely mentions "rate limiting",
    # "throttle", "overloaded", "quota". With NO explicit error flag, that text must NOT be
    # inferred as a transient error and bump the chain — it's regular coding-agent output.
    for noisy in (
        "I implemented rate limiting middleware with a throttle()",
        "the method is overloaded; set the disk quota",
        "this needs great capacity for the cache",
    ):
        out, _, _ = _run_hook(
            _event(task_id="noisy", output=noisy),
            monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
        )
        payload = json.loads(out)
        # No outcome signal -> a plain allow with no fallback block, and no state written.
        assert payload["decision"] == "allow"
        assert "fallback" not in payload
    assert not (tmp_path / "noisy.json").exists()


def test_hook_no_message_and_no_state_file_on_success_at_top(tmp_path, monkeypatch):
    # A clean turn on the preferred model is the normal case: no per-turn message (context
    # noise) and no state file littering the dir.
    out, _, _ = _run_hook(_event(task_id="top", success=True), monkeypatch, tmp_path)
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    assert "message" not in payload  # quiet success — no surfaced note
    assert not (tmp_path / "top.json").exists()


def test_hook_invalid_env_threshold_falls_back_to_default(tmp_path, monkeypatch):
    # A garbage AGENTTOOLS_FALLBACK_THRESHOLD must not switch on the first error (default 3).
    out, _, _ = _run_hook(
        _event(task_id="badthr", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "not-a-number"},
    )
    fb = json.loads(out)["fallback"]
    assert fb["switched"] is False
    assert fb["active"] == "claude:fable"


def test_hook_handles_non_dict_args(tmp_path, monkeypatch):
    # A malformed event whose `args` is not a dict must not crash the hook.
    event = {"hook_api": "agents-hooks/v1", "args": "oops"}
    out, _, code = _run_hook(event, monkeypatch, tmp_path)
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_threshold_helper_defaults_on_bad_values(monkeypatch):
    for bad in ("abc", "", "0", "-2"):
        monkeypatch.setenv("AGENTTOOLS_FALLBACK_THRESHOLD", bad)
        assert hook._threshold() == hook.DEFAULT_THRESHOLD
    monkeypatch.setenv("AGENTTOOLS_FALLBACK_THRESHOLD", "2")
    assert hook._threshold() == 2


def test_hook_survives_unparseable_stdin(tmp_path, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("AGENTTOOLS_FALLBACK_STATE_DIR", str(tmp_path))
    code = hook.main()
    assert code == 0  # advisory: fail-open
    assert json.loads(out.getvalue())["decision"] == "allow"


@pytest.mark.parametrize("payload", ["[]", "5", '"x"', "true", "null"])
def test_hook_survives_valid_non_object_json(payload, tmp_path, monkeypatch):
    # json.load accepts a top-level scalar/array; the hook must still fail-open, not raise
    # AttributeError on event.get(...).
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("AGENTTOOLS_FALLBACK_STATE_DIR", str(tmp_path))
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


@pytest.mark.parametrize("file_body", ['42', '"x"', '[1,2,3]', '{"index": [1,2]}', 'not json'])
def test_hook_resets_on_corrupt_state_file_without_crashing(file_body, tmp_path, monkeypatch):
    # A corrupt state file (non-dict JSON, list-typed index, or non-JSON) must reset to the
    # top and keep going (fail-open) — never crash the hook with a traceback.
    (tmp_path / "cf.json").write_text(file_body)
    out, _, code = _run_hook(
        _event(task_id="cf", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "2"},
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    # Reset to the top: this is the 1st error post-reset, threshold 2, so no switch yet.
    assert payload["fallback"]["active"] == "claude:fable"
    assert payload["fallback"]["switched"] is False


def test_hook_surfaces_exhausted_flag(tmp_path, monkeypatch):
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    last = None
    for _ in range(6):  # fall through all 5 steps, then one more error at the last resort
        raw, _, _ = _run_hook(
            _event(task_id="exh", error=True, error_text="429"), monkeypatch, tmp_path, env
        )
        last = json.loads(raw)
    assert last["fallback"]["exhausted"] is True
    assert last["fallback"]["active"] == "omp:k3"


def test_hook_runs_stateless_when_state_dir_cannot_be_created(tmp_path, monkeypatch):
    # If the state dir can't be made (mkdir raises), the hook must NOT crash — it runs
    # stateless (fresh state each turn) and still allows + reports.
    def boom_mkdir(self, *a, **k):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(Path, "mkdir", boom_mkdir)
    out, _, code = _run_hook(
        _event(task_id="nostate", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    # Stateless: a single error this turn at threshold 1 still switches within the turn.
    assert payload["fallback"]["switched"] is True


def test_hook_fail_open_when_state_file_is_unreadable(tmp_path, monkeypatch):
    # A state file that exists but raises on read (e.g. PermissionError) must reset-and-allow,
    # not crash. Simulate by making read_text raise OSError for the state file.
    (tmp_path / "ro.json").write_text('{"index": 2, "threshold": 1}')
    real_read_text = Path.read_text

    def boom_read(self, *a, **k):
        if self.name == "ro.json":
            raise PermissionError("no read")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom_read)
    out, _, code = _run_hook(
        _event(task_id="ro", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "2"},
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "allow"
    assert payload["fallback"]["active"] == "claude:fable"  # reset to the top


def test_hook_truthy_success_triggers_recovery(tmp_path, monkeypatch):
    # `success: 1` (truthy, not literal True) must still be recognised as a clean turn and
    # recover, symmetric with how `error` accepts any truthy.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="ts", error=True, error_text="429"), monkeypatch, tmp_path, env)
    raw, _, _ = _run_hook(_event(task_id="ts", success=1), monkeypatch, tmp_path, env)
    fb = json.loads(raw)["fallback"]
    assert fb["kind"] == "recover"
    assert fb["active"] == "claude:fable"


def test_hook_explicit_error_wins_over_success(tmp_path, monkeypatch):
    # A coarse host that sends BOTH success=True and error=True (+ a transient error_text) on a
    # turn that errored: the error must win so the fallback still fires. If success won, the
    # count would never grow and the feature would be dead.
    out, _, _ = _run_hook(
        _event(task_id="both", success=True, error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    fb = json.loads(out)["fallback"]
    assert fb["switched"] is True  # error wins -> the chain advanced
    assert fb["active"] == "claude:opus"


def test_hook_explicit_error_false_with_transient_text_is_a_clean_turn(tmp_path, monkeypatch):
    # An EXPLICIT error=False suppresses inference even when error_text looks transient — the
    # host is authoritative ("this turn was fine"), so it recovers rather than counting.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="ef2", error=True, error_text="429"), monkeypatch, tmp_path, env)  # -> opus
    raw, _, _ = _run_hook(
        _event(task_id="ef2", error=False, error_text="429 rate limit"), monkeypatch, tmp_path, env
    )
    fb = json.loads(raw)["fallback"]
    assert fb["kind"] == "recover"  # explicit error=False wins -> clean turn -> recover
    assert fb["active"] == "claude:fable"


def test_hook_model_error_truthy_fires_even_when_error_is_false(tmp_path, monkeypatch):
    # error and model_error are read symmetrically: a host whose `error` defaults to False but
    # signals via `model_error: True` must still fall back (not be read as a clean turn).
    out, _, _ = _run_hook(
        _event(task_id="me2", error=False, model_error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "1"},
    )
    fb = json.loads(out)["fallback"]
    assert fb["switched"] is True
    assert fb["active"] == "claude:opus"


def test_hook_success_with_nontransient_error_is_a_noop_not_recovery(tmp_path, monkeypatch):
    # A strange host sending BOTH success=True AND error=True with a NON-transient detail:
    # the explicit error wins over success, but the detail is non-transient, so the turn is a
    # no-op for the chain (no recovery). Documents the precedence + classification interaction.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="se", error=True, error_text="429"), monkeypatch, tmp_path, env)  # -> opus
    raw, _, _ = _run_hook(
        _event(task_id="se", success=True, error=True, error_text="AssertionError: boom"),
        monkeypatch, tmp_path, env,
    )
    fb = json.loads(raw)["fallback"]
    assert fb["switched"] is False
    assert fb["active"] == "claude:opus"  # stays put: not advanced, not recovered


def test_load_chain_falls_back_when_pyyaml_missing(monkeypatch):
    # If PyYAML can't be imported inside _load_chain, the broad except must still degrade to
    # DEFAULT_CHAIN rather than crashing the Stop hook.
    import builtins

    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no pyyaml")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    chain = hook._load_chain()
    assert [s.label for s in chain] == [s.label for s in fc.DEFAULT_CHAIN]


def test_hook_in_harness_recovery_has_no_prefer_next(tmp_path, monkeypatch):
    # opus -> fable recovery stays in the claude harness, so there is no cross-harness target:
    # neither redispatch_to nor prefer_next should appear.
    env = {"AGENTTOOLS_FALLBACK_THRESHOLD": "1"}
    _run_hook(_event(task_id="ih", error=True, error_text="429"), monkeypatch, tmp_path, env)  # fable->opus
    raw, _, _ = _run_hook(_event(task_id="ih", success=True), monkeypatch, tmp_path, env)  # opus->fable
    fb = json.loads(raw)["fallback"]
    assert fb["kind"] == "recover"
    assert fb["crosses_harness"] is False
    assert "prefer_next" not in fb
    assert "redispatch_to" not in fb


def test_hook_surfaces_subthreshold_retry_message(tmp_path, monkeypatch):
    # Below the threshold the hook should surface "N/threshold transient errors…" so the agent
    # knows it's retrying before any switch.
    out, _, _ = _run_hook(
        _event(task_id="sub", error=True, error_text="429"),
        monkeypatch, tmp_path, env={"AGENTTOOLS_FALLBACK_THRESHOLD": "3"},
    )
    payload = json.loads(out)
    assert payload["fallback"]["switched"] is False
    assert "1/3 transient errors" in payload["message"]
