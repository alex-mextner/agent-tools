#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw `gh pr merge` that bypasses ship.

Denies a shell command that merges a PR directly with `gh pr merge` (including
`gh pr merge --admin`), because that skips the project's ship gate: `gh ship <PR>`
(the green-CI-gated merge + mandatory-screenshot command) is the only sanctioned path.
A raw merge lands code without the green-CI check and the required-screenshot check —
exactly the gates ship exists to enforce.

This is the enforcement counterpart of the doc-only "use `gh ship`, never a raw merge"
rule: advice in AGENTS.md cannot stop an autonomous (auto-mode) agent from running
`gh pr merge`, so the rule only becomes real as a mid-session guard.

Detection is ARGV-BASED, not a raw substring match.  The command is parsed via shlex
into shell segments (`;` / `&&` / `||` / `|` separated), and each segment is inspected
for `basename(argv[0])=="gh" && argv[1]=="pr" && argv[2]=="merge"` after stripping
leading inline VAR=value assignments and shell grouping tokens (`(`, `{`).  Using
basename handles path-qualified invocations (`/usr/local/bin/gh pr merge`).  This
prevents a false positive where the body of an unrelated command (e.g. `gh pr create
--body "run gh pr merge to land it"`) triggers the guard — with a raw regex that body
text would match and the command would be blocked even though no actual merge is
occurring.

Allowed (let through):
  - `gh ship <PR>` / a `gh alias` that runs ship
  - `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
  - any non-merge `gh pr` subcommand (view, list, checkout, create, comment, ...)
  - commands whose body/arguments merely contain the text "gh pr merge" as a string

Escape hatch (controllable, not a hard wall — mirrors enforce-timeout / block-process-env):
  - env  ALLOW_RAW_PR_MERGE=1            — disable the guard for this session
  - env  ALLOW_RAW_PR_MERGE_REASON=...   — REQUIRED with the override; the reason is logged
  - inline sentinel  `# no-ship-guard: <reason>` anywhere in the command also overrides,
    so a one-off deliberate merge is self-documenting in the command itself.
  An override with no reason still blocks: a silent bypass of the bypass-guard is the very
  thing this hook exists to prevent.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a raw merge slipping through a
broken guard is exactly the failure this hook exists to stop.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Shell operators that separate independent command segments in a compound command line.
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})

# Shell grouping/control-flow tokens that may precede the real command name in a segment.
# E.g. `( gh pr merge 5 )` or `{ gh pr merge 5; }`.
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Merge-hint pattern: used to gate fail-closed on unbalanced-quote parse errors so that
# a benign command with an unbalanced quote (e.g. `grep won't file`) is NOT blocked just
# because shlex can't parse it.  Only if the raw string plausibly contains a merge
# invocation do we treat a parse error as fail-closed.
_MERGE_HINT = re.compile(r"\bgh\b.*\bpr\b.*\bmerge\b", re.DOTALL)

# Inline, self-documenting per-command override (checked on the raw string so a
# comment stripped by shlex is still found).
INLINE_SENTINEL = re.compile(r"#\s*no-ship-guard:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


def _split_segments(command: str) -> list[list[str]]:
    """Tokenize `command` via shlex and split into per-segment token lists.

    Uses ``punctuation_chars=True`` so that `;`, `&&`, `||`, `|` etc. are
    returned as standalone separator tokens rather than being glued to adjacent
    words (the default shlex.split() behaviour leaves `done;` as one token,
    missing the `;` separator entirely).

    Each segment is one independent command (the shell atoms between separators).
    Returns a list of token lists; raises ValueError on unbalanced quotes
    (caller treats this as fail-closed).
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = False
    tokens = list(lex)  # raises ValueError on unbalanced quotes
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _segment_argv(segment: list[str]) -> list[str]:
    """Return the real argv by stripping leading shell noise and VAR=value assignments.

    Strips leading grouping/control tokens (`(`, `{`, `!`, …) so that
    `( gh pr merge 5 )` or `{ gh pr merge 5; }` are correctly detected.
    Then strips leading VAR=value inline env assignments so that
    `GH_TOKEN=x gh pr merge 5` is correctly detected.
    """
    i = 0
    while i < len(segment) and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _is_gh_pr_merge(segment: list[str]) -> bool:
    """Return True iff this segment's argv is a `gh pr merge` invocation.

    Uses ``os.path.basename`` on argv[0] so that path-qualified invocations
    such as ``/opt/homebrew/bin/gh pr merge 5`` are correctly detected.
    """
    argv = _segment_argv(segment)
    return (
        len(argv) >= 3
        and os.path.basename(argv[0]) == "gh"
        and argv[1] == "pr"
        and argv[2] == "merge"
    )


def _command_contains_gh_pr_merge(command: str) -> bool | None:
    """Return True if any parsed segment of `command` is a `gh pr merge` call.

    Returns None (fail-closed) when the command cannot be parsed AND the raw
    text looks like a merge invocation.  Commands whose parse fails but contain
    no merge-like pattern (e.g. ``grep won't file`` with an unbalanced quote)
    return False so they are not spuriously blocked.
    """
    try:
        segments = _split_segments(command)
    except ValueError:
        # Fail-closed ONLY if the raw text plausibly contains a merge attempt.
        if _MERGE_HINT.search(command):
            return None
        return False
    return any(_is_gh_pr_merge(seg) for seg in segments)


def _override_reason(command: str) -> str | None:
    """Return the override reason if a valid escape hatch is present, else None.

    An override is honored ONLY with a reason: env ALLOW_RAW_PR_MERGE=1 plus
    ALLOW_RAW_PR_MERGE_REASON, OR an inline ``# no-ship-guard: <reason>`` sentinel.
    A reasonless override is ignored (the merge stays blocked).
    """
    if os.environ.get("ALLOW_RAW_PR_MERGE") == "1":
        reason = (os.environ.get("ALLOW_RAW_PR_MERGE_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-raw-pr-merge: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    is_merge = _command_contains_gh_pr_merge(command)

    if is_merge is None:
        warn("could not parse command (unbalanced quotes) — blocking (fail-closed)")
        emit(
            "block",
            "block-raw-pr-merge: command has unbalanced quotes — cannot verify "
            "it is not a raw merge, blocking (fail-closed).",
        )
        return BLOCK_EXIT_CODE

    if not is_merge:
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"raw `gh pr merge` allowed via escape hatch ({reason})")
        emit("allow", f"raw `gh pr merge` allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        "Refusing a raw `gh pr merge` (incl. --admin): it bypasses the ship gates "
        "(green CI + required screenshots). Use `gh ship <PR>` instead — it merges only "
        "once CI is green and the mandatory screenshots are present. Override only with an "
        "explicit reason: set ALLOW_RAW_PR_MERGE=1 and ALLOW_RAW_PR_MERGE_REASON='why', or "
        "append `# no-ship-guard: why` to the command.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
