#!/usr/bin/env python3
"""One-time Telegram hatch escalation for ship's review-quorum gate (Guard-B).

Reached at runtime ONLY from ci/ship/ship.sh, when the review-quorum bar is not met (or cannot
be verified) AND the shipper set RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM. It delegates to the shared
agenttools_hatch_escalation lib — the SAME lib the pin-primary-worktree / block-reset-hard
agent-hooks use — to ask Alex live on Telegram (`tg-ctl ask`) and returns his real-time verdict.

Invariants (why a bypass here can't be self-authorized):
  * The shared lib is imported from a FIXED path (lib/ two levels above this file), never an env
    var — so a shipper can't point it at an always-approve stub.
  * tg-ctl is resolved by the lib from the account's REAL home (pwd.getpwuid(os.getuid()).pw_dir),
    NOT the $HOME env var and NOT the repo being merged. So neither an ambient env var nor a
    rig.yaml a PR commits can redirect approval; only a rig.yaml in the real account home (a
    global, reviewed location) may override tg-ctl, else the lib's hard-coded trusted-paths
    allowlist (the real tg-ctl) is used.
  * This module writes its OWN audit line for the hatch outcome (bypass:approved / bypass:denied),
    so the record is single-source and testable in-process.

Past bug: an earlier revision resolved tg-ctl from the repo worktree (cwd=os.getcwd()) and then
from $HOME — both shipper/PR-controllable, letting a committed or env-pointed rig.yaml self-approve
(codex review P0/P1 on PR #225). Fixed by keying resolution off the OS identity's home only.

Exit codes (read by ship.sh): 0 approved, 1 requested-but-not-approved (blank/bare/denied/
timeout), 2 not requested (should not happen — ship only calls this when the env var is set),
3 the shared lib could not be imported (fail-closed).
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

# Import the shared lib from a FIXED location relative to this file (ci/ship/ -> ../../lib),
# never an env var. If it can't be imported (e.g. this script was copied out of the checkout),
# fail closed with exit 3 so ship refuses rather than proceeding unverified.
_LIB_DIR = Path(__file__).resolve().parents[2] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

try:  # pragma: no cover - exercised via the detached-copy integration test
    import agenttools_hatch_escalation as hatch_escalation
except Exception as exc:  # noqa: BLE001 - any import failure must fail closed
    sys.stderr.write(f"hatch escalation lib unavailable: {exc}")
    raise SystemExit(3)

_HOOK_ID = "ship-review-quorum"


def resolve_home() -> str:
    """The account's REAL home directory, from the OS identity — deliberately NOT $HOME.

    tg-ctl resolution walks up from this dir for a rig.yaml `agent_hooks.tg_ctl_path` override, so
    keying off getpwuid (not the env) means a shipper cannot redirect approval by exporting a
    doctored HOME. Overridable in tests via monkeypatch (never via the environment)."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _audit(decision: str, reason: str) -> None:
    """Append one hatch-outcome audit line to SHIP_AUDIT_FILE. Best-effort: a logging failure
    must never change the ship decision, so all errors are swallowed. Mirrors the JSON shape of
    ship.sh's _review_quorum_audit_log (ts/pr/task_code/iterations/models/decision + reason)."""
    path = os.environ.get("SHIP_AUDIT_FILE") or str(Path(resolve_home()) / ".config" / "agent-tools" / "ship-audit.jsonl")
    try:
        import datetime
        import json

        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pr": os.environ.get("SHIP_HATCH_PR", ""),
            "task_code": os.environ.get("SHIP_HATCH_CODE", ""),
            "iterations": int(os.environ.get("SHIP_HATCH_ITER", "0") or 0),
            "models": int(os.environ.get("SHIP_HATCH_MODELS", "0") or 0),
            "decision": decision,
        }
        if reason:
            rec["override_reason"] = reason
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


def main() -> int:
    ctx = {
        "pr": os.environ.get("SHIP_HATCH_PR", ""),
        "task_code": os.environ.get("SHIP_HATCH_CODE", ""),
        "iterations": os.environ.get("SHIP_HATCH_ITER", ""),
        "distinct_models": os.environ.get("SHIP_HATCH_MODELS", ""),
        "repo": os.getcwd(),
        "gate": "ship review-quorum (self-merge-authority Guard-B)",
    }
    kw: dict[str, float] = {}
    timeout_s = _float_env("SHIP_HATCH_TIMEOUT_S")
    if timeout_s is not None:
        kw["timeout_s"] = timeout_s
    margin_s = _float_env("SHIP_HATCH_PROCESS_MARGIN_S")
    if margin_s is not None:
        kw["process_margin_s"] = margin_s

    result = hatch_escalation.request_hatch_approval(_HOOK_ID, ctx, cwd=resolve_home(), **kw)
    sys.stderr.write(result.reason or "")
    if result.approved:
        _audit("bypass:approved", result.reason or "")
        return 0
    if result.env_present:
        _audit("bypass:denied", result.reason or "")
        return 1
    return 2  # not requested — ship.sh only calls this when the env var is present


if __name__ == "__main__":
    raise SystemExit(main())
