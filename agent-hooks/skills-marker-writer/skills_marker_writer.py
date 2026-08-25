#!/usr/bin/env python3
"""agents-hooks/v1 pre-skill hook — write the skills-read-gate freshness marker.

skills-read-gate (the sibling hook) blocks/warns on work-shaped actions (a commit, a
build/test run) unless the MANDATORY_SKILLS were invoked recently. It decides "recently
invoked" by checking mtime freshness on a marker file per skill, nested under the CC
session id that invoked it:

    ~/.cache/agent-tools/skills-invoked/<session-id>/<skill-name>

Before this hook, NOTHING ever touched that directory — the gate's own README documented
the exact wrapper contract needed and said "wire the wrapper to touch it on every
Skill-tool invocation", but no code did it, so the freshness check always saw a missing
marker and the gate could never leave its WARN-forever tier (agent-tools issue: the
skills-read-gate marker contract was unsatisfiable by design until this hook existed).

This hook is the wrapper: it fires on every Skill-tool invocation (the `pre-skill` point,
mapped in lib/cc_hook_bridge/dispatch.py from CC's PreToolUse on the `Skill` tool) and
touches the marker for the invoked skill. It NEVER blocks — a marker-write failure must
never stop a skill from being invoked, so every code path here ends in `allow`.

Untrusted input: `args.skill` is model-controlled — the agent decides which skill to
invoke, and a legitimate directory-scoped skill name contains a `/` (e.g.
`apps/web:deploy`, per Claude Code's Skill-tool scoping). So a `/`-containing name is
expected and allowed to nest under the marker dir, but an absolute path, a `..` segment,
or an oversized/NUL-containing value must never be able to write outside
SKILLS_INVOKED_DIR — see `_sanitize_skill_name` / `_write_marker`.

Session scoping: the marker is additionally nested under `args.session_id` (CC's own
session id — lib/cc_hook_bridge/dispatch.py gives it the same T2 precedence as
`agent_id`, so a value riding in via `tool_input` cannot spoof it) when one is present and
sane, falling back to the pre-existing global (non-session) path otherwise. Without this,
`skills-read-gate`'s marker check is a single mtime file shared by every concurrent Claude
Code session on the machine — session A invoking a mandatory skill silently satisfies
session B's freshness check even though B never read it. `args.session_id` is treated as
ONE flat path component (never split on `/`) — see `_sanitize_session_id`.

Session-dir growth (NOT addressed here, deliberately deferred): every CC session mints its
own `<session-id>/` subdirectory and nothing removes it, so the marker dir grows by one
subdirectory per session forever. An earlier revision of this hook attempted in-process GC
here and was reverted — the design space (a per-pass time budget vs. a per-child scan bound
vs. the retention floor vs. `SKILLS_FRESH_WINDOW_S`, all interacting) turned out to need its
own focused PR rather than living inside this one; the accumulated findings are captured in
the follow-up ticket referenced from this PR. An external periodic sweep (cron/launchd)
outside the hook's own 800ms timeout budget is the leading candidate, not more in-hook GC.

Accepted tradeoff: this fires on PreToolUse — the ATTEMPT to invoke the skill, before CC
resolves/runs it — because that is the only reliably-shaped signal available (a
PostToolUse `tool_response` has no documented success/failure field to gate on here, and
speculatively parsing one would be worse than not trying). In practice this cannot
falsely satisfy skills-read-gate's freshness check for the WRONG reason: a typo'd or
nonexistent skill name writes a marker under that wrong name, not under the mandatory
skill's own name, so the gate still correctly sees the mandatory skill's marker as
missing. The only way this over-certifies is the mandatory skill's own name being invoked
correctly and the underlying invocation itself failing — a broken skill file, not an
agent behavior gap — and the gate is advisory (WARN-first, fail-open) rather than a
security boundary, so this residual case is accepted rather than engineered around.

Contract (agents-hooks/v1):
  stdin  : JSON event; the invoked skill name is in args.skill
  stdout : protocol JSON only       exit 0 : allow   (this hook never blocks)

on_error is "open": a marker-write hiccup is a missed freshness update, not a security
boundary — it must never wedge the ability to invoke a skill.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"

INVOKED_DIR = Path(os.path.expanduser(os.environ.get(
    "SKILLS_INVOKED_DIR", "~/.cache/agent-tools/skills-invoked")))

_MAX_SKILL_LEN = 200
_MAX_SESSION_ID_LEN = 128


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"skills-marker-writer: {msg}\n")


def _sanitize_skill_name(skill: str) -> list[str] | None:
    """Return `skill` split into path segments if it is safe to use as a relative path
    under SKILLS_INVOKED_DIR, else None. Rejects an absolute path, any `.`/`..` segment, an
    embedded NUL, an empty value, and anything past a sane length cap. A bare `/`-separated
    name (directory-scoped skill) is otherwise allowed through — nesting is intentional.
    Splits on plain `/`/`\\` — simpler than a regex for a two-character class, no `re`
    import needed (note: this does NOT meaningfully cut interpreter startup cost, since
    `pathlib` — already imported below — pulls in `re` itself on CPython <= 3.12)."""
    if not skill or len(skill) > _MAX_SKILL_LEN or "\x00" in skill:
        return None
    if skill.startswith("/") or skill.startswith("\\"):
        return None
    segments = skill.replace("\\", "/").split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None
    return segments


def _sanitize_session_id(session_id: str) -> str | None:
    """Return `session_id` if it is safe to use as a SINGLE path segment, else None (the
    caller falls back to the pre-existing global, non-session-scoped marker location).
    Unlike a skill name, a session id is never expected to contain `/` — CC generates it,
    not the model — so any `/`/`\\` in it is treated as a sign the value is not a real
    session id (or, defensively, an attempt to nest/escape) rather than something to
    split and nest under."""
    session_id = session_id.strip()
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN or "\x00" in session_id:
        return None
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        return None
    return session_id


def _write_marker(invoked_dir: Path, segments: list[str], session_seg: str | None = None) -> bool:
    """Touch the freshness marker at `segments` (already validated by
    `_sanitize_skill_name`), nested under `session_seg` when present (already validated by
    `_sanitize_session_id`). Returns False (never raises) on any failure — a permission
    error, a race, or a resolved path that would escape `invoked_dir` (belt and suspenders
    on top of the caller's sanitization, in case a future caller skips it)."""
    parts = ([session_seg] if session_seg else []) + segments
    candidate = invoked_dir.joinpath(*parts)
    try:
        resolved_dir = invoked_dir.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        warn(f"could not resolve marker path for {parts!r}: {exc}")
        return False
    if resolved_candidate != resolved_dir and resolved_dir not in resolved_candidate.parents:
        warn(f"refusing to write marker outside {resolved_dir}: {parts!r} -> {resolved_candidate}")
        return False
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        # exist_ok=True: on an already-existing file, Path.touch() itself calls
        # os.utime(path, None) first (CPython, 3.4+) — that IS the freshness bump the
        # marker contract needs, so there is nothing left to do after this call.
        candidate.touch(exist_ok=True)
    except OSError as exc:
        warn(f"could not write marker {candidate}: {exc}")
        return False
    return True


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # Valid JSON that isn't an object (`[]`, `5`, `"x"`, `true`, `null`) parses fine but has
    # no `.get` — guard explicitly rather than relying on `event.get("args") or {}` alone,
    # which only rescues FALSY non-dicts (`[]`, `""`, `0`) and still crashes on a truthy one
    # (e.g. `[1]`).
    if not isinstance(event, dict):
        emit("allow")
        return 0

    args = event.get("args")
    if not isinstance(args, dict):
        emit("allow")
        return 0
    skill = args.get("skill")
    if not isinstance(skill, str):
        emit("allow")  # no skill name on this event → nothing to record
        return 0

    segments = _sanitize_skill_name(skill.strip())
    if segments is None:
        warn(f"skill name failed sanitization, not writing a marker: {skill!r}")
        emit("allow")
        return 0

    raw_session_id = args.get("session_id")
    session_seg = (
        _sanitize_session_id(raw_session_id) if isinstance(raw_session_id, str) else None
    )

    _write_marker(INVOKED_DIR, segments, session_seg)  # best-effort; never blocks on failure
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
