#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — keep long-running processes off the main thread.

A long-running process started inline by the ORCHESTRATOR blocks the main thread for
minutes — a multi-model `review`, a `--watch` loop, a full build/test suite, a long
`sleep`. This gate hard-BLOCKs such a command and tells the orchestrator to dispatch it
to a BACKGROUND subagent instead. It enforces the "orchestrator stays responsive" half of
`delegate-work-to-subagents` (the most clear-cut case → straight block, not warn-first).

Matched (conservative, anchored so a substring in a path/word never trips it):
  - the `review` CLI invoked as a command (start, or after && / ; / |) — minutes-long
  - any `--watch` flag (gh pr checks --watch, vitest --watch, tsc --watch, …)
  - a build/test SUITE: npm/pnpm/yarn/bun test|build, pytest/vitest/jest/cypress/playwright,
    cargo test|build, go test|build, make test|build|all
  - `sleep N[unit]` with a duration >= 10s (`sleep 5m` / `sleep 1h` count; `sleep 2` is fine)

Subagent-exempt: a dispatched subagent (``agent_id`` present) is EXPECTED to run these in
the background, so it is always allowed — this gate governs the orchestrator only.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_INLINE_PROCESS=1            — disable the guard for this session
  - env  ALLOW_INLINE_PROCESS_REASON=...   — REQUIRED with the override; logged
  - inline  `# inline-process-ok: <reason>`  — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A command starts at the line start or right after a &&/;/|/( separator. We anchor the
# long-process patterns on this so `review` inside a path (`/src/review/x`) or as a flag
# value never trips the guard — only an actual command invocation does.
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"

# The `review` CLI invoked as a command (multi-model, minutes-long).
REVIEW = re.compile(_CMD_START + r"review\b")
# Any --watch flag anywhere (gh pr checks --watch, vitest --watch, tsc --watch, …).
WATCH = re.compile(r"--watch\b")
# A build/test SUITE (each anchored at a command start to avoid substring false hits).
SUITE = re.compile(
    _CMD_START + r"(?:npm|pnpm|yarn|bun|deno)\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:pytest|vitest|jest|cypress|playwright)\b"
    r"|" + _CMD_START + r"cargo\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"go\b[^&;|]*\b(?:test|build)\b"
    r"|" + _CMD_START + r"(?:make|rake|msbuild)\b[^&;|]*\b(?:test|build|all)\b"
    r"|" + _CMD_START + r"(?:mvn|gradle)\b[^&;|]*\b(?:test|build|verify|package)\b"
)
# `sleep N[unit]` invoked as a command (>= 10s is "long"). Anchored at a command start so
# `echo "sleep 100"` (the word inside a string) never trips it. The optional unit suffix
# (s/m/h/d) is parsed to seconds, so `sleep 5m` / `sleep 1h` are correctly seen as long —
# a bare `\d+` would read `5m` as 5 and wave a five-minute sleep through.
SLEEP = re.compile(_CMD_START + r"sleep\s+(\d+(?:\.\d+)?)([smhd]?)\b")
_SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
INLINE_SENTINEL = re.compile(r"#\s*inline-process-ok:\s*(\S.*)")

# Leading command WRAPPERS that prefix the real command without changing what it does
# (`timeout 600 npm test`, `env CI=1 pytest`, `nice -n10 review`, `time make build`). The
# real runner sits AFTER the wrapper + its args, so the `_CMD_START`-anchored patterns above
# miss it unless we peel the wrapper off first. ``_unwrap_segment`` knows how to skip each
# wrapper's OWN args before the wrapped command begins:
#   - timeout DURATION cmd…           → the duration positional + -s/-k/--signal/--kill-after
#   - env [NAME=val…|-i|-u N] cmd…    → flags + KEY=VALUE assignments skipped
#   - nice [-n N] cmd… / time cmd…    → nice's -n N skipped; time takes no positional
#   - stdbuf -oL cmd… / nohup cmd… / setsid cmd… / unbuffer cmd… → flags-only, no positional
# Deliberately conservative: wrappers with a POSITIONAL arg we don't model (chrt's RTPRIO,
# ionice's `-c CLASS`) are LEFT OUT — peeling them only partway would leave that positional at
# the head and silently MISS the long process (a false-negative is worse than not unwrapping).
_WRAPPERS = re.compile(r"^(?:timeout|env|nice|time|stdbuf|nohup|setsid|unbuffer)$")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-long-inline-process: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_INLINE_PROCESS") == "1":
        reason = (os.environ.get("ALLOW_INLINE_PROCESS_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _unwrap_segment(segment: str) -> str:
    """Strip leading no-op wrappers (`timeout 600`, `env CI=1`, `nice -n10`, `time`) from one
    command segment, returning the wrapped command. Peels repeatedly (`time timeout 5m review`).

    Skips, for a recognized wrapper: its option flags (`-k`, `--signal`, and their separate
    values), and — for `env` — leading `KEY=VALUE` assignments. A flag joined to its value
    (`-n10`, `--kill-after=5`) consumes no extra token. A non-wrapper segment is returned
    unchanged (the while-guard below never fires)."""
    tokens = segment.split()
    changed = True
    while changed and tokens and _WRAPPERS.match(tokens[0]):
        changed = False
        wrapper, rest = tokens[0], tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                # `--signal SIGTERM` / `-k 5` take a separate value; `-n10` / `--kill=5` don't.
                if "=" not in tok and tok in ("-s", "-k", "--signal", "--kill-after", "-n", "-u"):
                    i += 1
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional (the wrapped command follows)
            break
        if i:  # we consumed at least the wrapper's own args → real command starts at rest[i:]
            tokens = rest[i:]
            changed = True
        elif rest:  # bare wrapper with no args of its own (e.g. `time make build`, `nohup cmd`)
            tokens = rest
            changed = True
    return " ".join(tokens)


def _unwrap_command(command: str) -> str:
    """The command with leading wrappers peeled off EACH `&&`/`;`/`|`-separated segment, so a
    wrapped runner (`timeout 600 npm test`, `x && env CI=1 pytest`) is exposed at the segment
    head where the `_CMD_START`-anchored patterns can see it."""
    parts = re.split(r"(\s*(?:&&|\|\||;|\|)\s*)", command)
    return "".join(
        seg if i % 2 else _unwrap_segment(seg.strip())
        for i, seg in enumerate(parts)
    )


def _matched_long_process(command: str) -> str | None:
    """Return a short label of the matched long-running process, or None.

    Each anchored pattern is tried against BOTH the raw command AND an unwrapped form (leading
    wrappers like `timeout`/`env`/`nice`/`time` peeled off each segment). Unwrapping can only ADD
    matches it would otherwise miss (`timeout 600 npm test`, `env CI=1 pytest`, `timeout 5m
    review`) — never remove one the raw string would have caught, so wrapper-parsing slips or
    whitespace normalization can't open a hole."""
    unwrapped = _unwrap_command(command)

    def hit(pat: re.Pattern[str]) -> bool:
        return bool(pat.search(command) or pat.search(unwrapped))

    if hit(REVIEW):
        return "review (multi-model, minutes-long)"
    if hit(WATCH):  # --watch can appear anywhere; raw is enough but unwrapped is harmless
        return "--watch (a watch loop never exits)"
    if hit(SUITE):
        return "a build/test suite"
    m = SLEEP.search(command) or SLEEP.search(unwrapped)
    if m:
        seconds = float(m.group(1)) * _SLEEP_UNIT_S[m.group(2)]
        if seconds >= 10:
            return f"sleep {m.group(1)}{m.group(2)} (long sleep)"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent is EXPECTED to run these in the background → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    matched = _matched_long_process(command)
    if matched is None:
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"inline long process allowed via escape hatch ({reason})")
        emit("allow", f"inline long process allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        f"Run this in a BACKGROUND subagent, not the orchestrator: `{matched}` is a "
        "long-running process (review / --watch / build-or-test suite / long sleep) that "
        "would block the main thread. Dispatch an Agent with run_in_background: true to run "
        "it. Override only with a reason: ALLOW_INLINE_PROCESS=1 + "
        "ALLOW_INLINE_PROCESS_REASON='why', or append `# inline-process-ok: why`.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
