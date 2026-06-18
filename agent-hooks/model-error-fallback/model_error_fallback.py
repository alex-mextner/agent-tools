#!/usr/bin/env python3
"""agents-hooks/v1 hook — auto-fall-back down the model chain on repeated model errors.

What it does
------------
Counts CONSECUTIVE transient model errors (rate-limit / overload / API 5xx) for a unit of
work and, past a threshold, switches the active executor down the cross-harness chain
``claude:fable -> claude:opus -> oc:GLM-5.2 -> codex:gpt5.5``. Within a harness the switch
is a model swap; across the boundary it tells the host to RE-DISPATCH the work to the next
harness (opencode / codex exec) as an executor. On recovery it promotes back toward the
preferred model (return-to-top). All the count/threshold/switch/recover logic is the pure
:mod:`fallback_chain` module next to this file; this script is only the I/O shell.

The chain is read from ONE definition — ``lib/contracts/models.yaml`` ``fallback_chain:`` —
so every harness (cc/codex/oc/pi) agrees on the order; the baked-in default is the offline
fallback.

Contract (agents-hooks/v1)
--------------------------
  stdin  : JSON event. The turn outcome is read from a few keys (host-mapped):
           args.error / args.model_error (bool, truthy = error), args.success (bool),
           args.error_text / args.detail (the ERROR channel, classified transient-or-not).
           args.output / args.message are accepted but DELIBERATELY NOT classified (a Stop
           hook's output is the agent's normal text). The unit-of-work id is
           args.task_id / event.event_id / args.session_id (state is keyed by it).
  stdout : protocol JSON. This is an ADVISORY hook — it always `allow`s; it never blocks a
           turn. The switch instruction rides in the `message` (surfaced to the model) and a
           `fallback` block (machine-readable: active step, kind, redispatch target).
  stderr : human logs.
  exit 0 : allow (always).

on_error is "open": this hook must NEVER wedge a turn. If it can't read state or classify
the outcome, it warns and allows — the worst case is a missed switch, recovered on the next
error, which is strictly better than blocking the agent.

State
-----
Persisted as a small JSON file under a per-unit-of-work key so the consecutive-error count
survives across turns of the same task. The dir is ``$AGENTTOOLS_FALLBACK_STATE_DIR`` or a
temp-dir default; a missing/corrupt file resets to the top of the chain (logged), which is
safe — at worst the chain is re-walked from the preferred model.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fallback_chain as fc  # noqa: E402

HOOK_API = "agents-hooks/v1"
# Default consecutive-error count before falling to the next step, when the env override is
# absent. Mirrors the review-cli retry budget (ROADMAP "review resilience": ~3 retries before
# declaring a seat failed).
DEFAULT_THRESHOLD = 3


def _threshold() -> int:
    """How many consecutive transient errors before switching — read at CALL time.

    Read here, not as an import-time constant, so the env override
    (``AGENTTOOLS_FALLBACK_THRESHOLD``) takes effect per invocation. A non-integer/zero value
    falls back to the default rather than disabling the switch.
    """
    raw = os.environ.get("AGENTTOOLS_FALLBACK_THRESHOLD", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if value >= 1 else DEFAULT_THRESHOLD


def emit(decision: str, message: Optional[str] = None, fallback: Optional[dict] = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    if fallback:
        out["fallback"] = fallback
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"model-error-fallback: {msg}\n")


def _state_dir() -> Optional[Path]:
    """The state directory, or None if it can't be created (then we run without persistence).

    A failure to make the dir (read-only fs, permissions) must NOT crash the hook — it
    degrades to a stateless run (each turn starts fresh from the top), which is fail-open and
    strictly better than a traceback.
    """
    override = os.environ.get("AGENTTOOLS_FALLBACK_STATE_DIR")
    base = Path(override) if override else Path(tempfile.gettempdir()) / "agenttools-fallback"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn(f"state dir unavailable ({exc}) — running without cross-turn persistence")
        return None
    return base


def _safe_key(raw: str) -> str:
    """A filesystem-safe, single-segment state key from a unit-of-work id (default 'default').

    Non-alphanumeric characters (path separators included) collapse to ``_``; a leading dot
    is stripped so the key can't start a dotfile or be the traversal segments ``.``/``..``.
    The result is always a plain filename in the state dir, never a path.
    """
    cleaned = "".join(c if c.isalnum() or c in "-._" else "_" for c in (raw or "")).strip("_")
    cleaned = cleaned.lstrip(".")  # no leading dot -> no '.', '..', or hidden-file key
    return cleaned or "default"


def _load_chain() -> tuple:
    """The fallback chain: the manifest's ``fallback_chain:`` if readable, else the default.

    Reading the manifest keeps ONE chain definition shared by every harness. PyYAML and the
    manifest are both optional at runtime — a host without them still gets the correct
    baked-in default, never an empty chain.
    """
    manifest = _HERE.parents[1] / "lib" / "contracts" / "models.yaml"
    if not manifest.exists():
        # A common, benign case (the script was copied out of the repo at install): log it
        # distinctly from a genuine parse/validation failure so an operator can tell "no
        # manifest here" from "manifest present but broken".
        warn(f"manifest not found at {manifest} — using built-in default chain")
        return fc.DEFAULT_CHAIN
    try:
        import yaml  # lazy: keeps this script importable without PyYAML

        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        steps = (raw or {}).get("fallback_chain")
        if steps:
            return fc.chain_from_manifest_steps(steps)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the default chain
        warn(f"using built-in chain (manifest present but unreadable: {exc})")
    return fc.DEFAULT_CHAIN


def _load_state(path: Path, chain: tuple, threshold: int) -> fc.FallbackState:
    """Load the persisted state for this unit of work, re-pinned to ``chain``.

    The LIVE ``threshold`` (from the env, read this invocation) always wins over the value
    saved in the snapshot, so ``AGENTTOOLS_FALLBACK_THRESHOLD`` takes effect even mid-task —
    the position/count carry over, the threshold is re-applied fresh each turn.
    """
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        # state_from_snapshot already pins to `chain` and clamps the saved index into it, so
        # no second re-pin is needed here. The live env threshold then overrides the saved one.
        state = fc.state_from_snapshot(snap, chain=chain)
        state.threshold = threshold
        return state
    except FileNotFoundError:
        return fc.FallbackState(chain=chain, threshold=threshold)
    except (
        OSError,  # PermissionError / IsADirectoryError / … — an unreadable file is fail-open
        json.JSONDecodeError,
        ValueError,
        TypeError,
        AttributeError,
        fc.FallbackError,
    ) as exc:
        # Any corrupt/unexpected snapshot OR an unreadable file resets to the top — the
        # fail-open contract. TypeError/AttributeError are the belt-and-suspenders for a shape
        # state_from_snapshot doesn't explicitly normalise (it raises FallbackError for the
        # known cases); OSError covers a file that exists but can't be read.
        warn(f"resetting fallback state (unreadable/corrupt snapshot {path.name}: {exc})")
        return fc.FallbackState(chain=chain, threshold=threshold)


def _save_state(path: Path, state: fc.FallbackState) -> None:
    # Write atomically (temp file in the same dir + os.replace) so a crash mid-write can't
    # leave a half-written JSON that the next turn would treat as corrupt and reset.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state.snapshot()), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        warn(f"could not persist fallback state ({exc}) — count may reset next turn")
        # Don't leave a stray temp file behind if the write succeeded but replace failed.
        try:
            tmp.unlink()
        except OSError:
            pass


def _clear_state(path: Path) -> None:
    """Remove a now-pristine state file (top of chain, zero pending). Best-effort.

    A missing file already reads as the default top-of-chain state, so dropping the file when
    the state returns to pristine keeps the state dir from accumulating one file per task id.
    """
    try:
        path.unlink()
    except OSError:
        pass  # a missing or unremovable file is harmless — the default is the same state


def _read_outcome(args: dict) -> tuple:
    """Extract ``(is_error, detail)`` from the event ``args``. ``is_error=None`` = 'no signal'.

    Two distinct text sources are kept apart on purpose:

    * the ERROR channel (``error_text`` / ``detail``) — a provider/error string the host
      passes when something went wrong;
    * the general text (``output`` / ``message``) — the agent's NORMAL response on a Stop
      hook, which routinely contains words like "rate limiting", "throttle", "overloaded",
      "quota" in legitimate output.

    The error detail used for transient-vs-normal classification is ALWAYS the error channel
    only — ``output`` / ``message`` never reach the classifier:

    * No explicit flag: an error is inferred ONLY from a transient-looking error channel, so a
      coding agent that merely *writes about* rate-limiting in its output isn't misread as
      hitting one and spuriously bumped down the chain.
    * Explicit ``error=True`` / ``model_error=True``: the error channel is the classified
      detail. If it's empty, the detail is empty too — which `observe` treats as transient
      ("trust the host's error flag"), NOT as a non-transient failure. The general
      ``output`` / ``message`` text is deliberately NOT substituted in, because classifying
      the agent's normal response would both swallow real errors (no keywords) and fire false
      ones (incidental keywords).
    """
    error_channel = str(args.get("error_text") or args.get("detail") or "")

    # `error` and `model_error` are read SYMMETRICALLY by truthiness: a truthy EITHER flag is
    # an explicit error. This matters for a host whose `error` defaults to False but signals a
    # real failure via `model_error` — reading only `error` would silently never fall back.
    err_flag = args.get("error")
    me_flag = args.get("model_error")
    error_is_truthy = (err_flag is not None and bool(err_flag)) or (
        me_flag is not None and bool(me_flag)
    )
    have_error_flag = err_flag is not None or me_flag is not None

    # An explicit truthy error wins over `success`. A coarse host (e.g. cc_hook_bridge mapping
    # `success` to "turn completed") might send BOTH success:true AND error on a turn that
    # errored; if `success` won, the count would never grow and the fallback would never fire.
    # A missed fallback is worse than a missed recovery, so the error takes precedence.
    if error_is_truthy:
        # Classify the error channel only. An empty channel -> empty detail -> observe() trusts
        # the flag and treats it as transient; output/message never count.
        return True, error_channel
    if "success" in args and bool(args.get("success")):
        return False, error_channel
    if have_error_flag:
        # An explicit error/model_error flag is present but falsy (and no success) -> clean turn.
        return False, error_channel
    # No explicit flag — infer ONLY from the error channel, never from output/message.
    if error_channel and fc.is_transient_model_error(error_channel):
        return True, error_channel
    return None, error_channel


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (advisory hook, fail-open)")
        emit("allow")
        return 0

    # json.load also accepts a valid NON-object payload ([], 5, "x", true) that never hits the
    # except above; guard so event.get(...) below can't raise AttributeError and break the
    # fail-open contract on a malformed-but-parseable event.
    if not isinstance(event, dict):
        warn("event is not a JSON object — allowing (advisory hook, fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    is_error, detail = _read_outcome(args)
    if is_error is None:
        # No outcome signal in this event — nothing to count. Allow, no-op.
        emit("allow")
        return 0

    key = _safe_key(
        str(args.get("task_id") or event.get("event_id") or args.get("session_id") or "")
    )
    chain = _load_chain()
    threshold = _threshold()

    # The state dir may be unavailable (read-only fs / permissions). If so, run STATELESS:
    # a fresh state this turn, no load, no save. Fail-open — a missed cross-turn count beats a
    # crash. With a dir, load/persist as usual.
    state_dir = _state_dir()
    state_path = state_dir / f"{key}.json" if state_dir is not None else None
    state = (
        _load_state(state_path, chain, threshold)
        if state_path is not None
        else fc.FallbackState(chain=chain, threshold=threshold)
    )
    decision = state.observe(error=bool(is_error), detail=detail)

    # Persist only when the RESULTING state is worth remembering, i.e. non-pristine: below
    # the top of the chain, or with errors accumulating. A pristine "top of chain, zero
    # pending" state is the implicit default — a missing file already reads as that — so we
    # don't write one (a healthy preferred model leaves no file). This is keyed off the state
    # itself, not `decision.switched`, so a recovery back TO the top removes the file in the
    # same turn rather than leaving a pristine file around for one extra turn.
    if state_path is not None:
        if state.index > 0 or state.consecutive_errors > 0:
            _save_state(state_path, state)
        else:
            _clear_state(state_path)

    fallback_block = {
        "active": decision.active.label,
        "harness": decision.active.harness,
        "model": decision.active.model,
        "kind": decision.kind,
        "switched": decision.switched,
        "crosses_harness": decision.crosses_harness,
        "exhausted": decision.exhausted,
        "reason": decision.reason,
    }
    if decision.crosses_harness:
        target = {"harness": decision.active.harness, "model": decision.active.model}
        if decision.kind == "fall":
            # A FALL means the work FAILED on the old executor: re-dispatch THIS unit of work
            # to the new harness as an executor (re-run it there). Safe — nothing completed.
            fallback_block["redispatch_to"] = target
        else:
            # A RECOVER fires AFTER a successful turn: the work is already done. Emitting a
            # `redispatch_to` here would make a naive host RE-RUN completed work (duplicate
            # side effects). Instead signal "prefer this executor for the NEXT unit of work"
            # — a preference change, not a re-dispatch of the finished one.
            fallback_block["prefer_next"] = target

    warn(decision.reason)
    # Surface a message to the model ONLY when there is something to act on: an actual switch
    # (swap / re-dispatch / recover) or accumulating errors ("2/3 transient errors…"). A
    # quiet success on the preferred model produces NO message — it is the normal case and a
    # per-turn "staying at the top" note would just be context noise on a Stop hook.
    noteworthy = decision.switched or state.consecutive_errors > 0
    message = f"model-error-fallback: {decision.reason}" if noteworthy else None
    emit("allow", message=message, fallback=fallback_block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
