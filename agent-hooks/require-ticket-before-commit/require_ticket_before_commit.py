#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require a ticket reference on a commit.

When the agent is about to `git commit`, this checks the commit message (and the
current branch name) for a reference to a tracking ticket — a task-cli id, a
GitHub issue, or a Linear key. If none is found, it WARNS (default) or, in strict
mode, BLOCKS, reminding the author that every non-trivial change should start from
a ticket with acceptance criteria + motivation + user-impact.

Enforces the `strict-ticket-discipline` skill. Pairs with task-cli.

Ticket-detection heuristic (intentionally broad — false-negatives nag, they don't
wedge; default fail policy is open/warn so over-detection is the cheap error):
  - `#123`                              GitHub issue / PR number
  - `GH-123`, `org/repo#123`           qualified GitHub references
  - `ABC-123`                          Linear / Jira-style KEY-NUM (>=2 letters)
  - `task:ABC-12`, `task #12`, `T-12`  task-cli ids
  - `Refs: …`, `Closes #…`, `Fixes …`  the conventional trailer keywords
  - a full tracker URL (github.com/.../issues/123, linear.app/.../issue/…)

Exempt from the gate (no ticket expected): trivial-chore commit types
(`chore:`/`docs:`/`style:`/`ci:`/`build:`/`test:`), and `wip`/`fixup!`/`squash!`
/`amend`/merge/revert commits. Configure via env (see README).

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": this is process discipline, not a security boundary — a crash
in the check must never make committing impossible. By default it ALSO only warns
(allows with an advisory message) even when it finds no ticket, so it can't
over-block; set REQUIRE_TICKET_STRICT=1 to turn a missing ticket into a hard block.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Strict mode turns a missing-ticket warning into a hard block.
STRICT = os.environ.get("REQUIRE_TICKET_STRICT") == "1"

GIT_COMMIT = re.compile(r"\bgit\b.*\bcommit\b")
# `git commit --continue/--abort/--skip` (merge/rebase plumbing) and `--amend`
# are not authoring a fresh change → don't gate them.
SKIP_COMMIT = re.compile(r"--(?:continue|abort|skip|amend)\b")

# Commit-type prefixes that are exempt by default (conventional-commit chores).
# Override the set with REQUIRE_TICKET_EXEMPT_TYPES="chore,docs" (comma-separated).
_DEFAULT_EXEMPT_TYPES = "chore,docs,style,ci,build,test,revert"
EXEMPT_TYPES = {
    t.strip()
    for t in os.environ.get("REQUIRE_TICKET_EXEMPT_TYPES", _DEFAULT_EXEMPT_TYPES).split(",")
    if t.strip()
}
# A conventional-commit header: `type(scope)!: subject`. Capture the bare type.
# Anchored at the start of the (first) line — the type only counts on the subject,
# not on some `test:`-looking line buried in the body.
CONVENTIONAL = re.compile(r"^\s*([a-z]+)(?:\([^)]*\))?!?:", re.IGNORECASE)

# WIP / fixup / merge / revert markers that mean "not a normal authored commit".
EXEMPT_MARKERS = re.compile(
    r"^\s*(?:wip\b|fixup!|squash!|amend!|merge\b|revert\b)", re.IGNORECASE
)

# --- Ticket-reference heuristic -------------------------------------------------
# Kept deliberately permissive: a missed reference at worst nags (fail-open/warn),
# while a false hit just lets a commit through that probably had a ticket anyway.
#
# A KEY-NUM id needs >=2 uppercase letters so it can't collide with a version like
# `A1-456` or a hyphenated word; a trailer keyword (Closes/Fixes/Refs) must be
# followed by a REAL ref, not any token, so `fix: null deref` is NOT a reference.
_KEY_NUM = r"[A-Z]{2,}[A-Z0-9]*-\d+"  # ABC-123, ENG-7, GH style keys
_REF = rf"(?:#\d+|{_KEY_NUM}|T-\d+)"  # the concrete things a trailer can point at
TICKET_PATTERNS = (
    re.compile(r"(?:^|\s|\()#\d+\b"),                       # #123 (GitHub issue/PR)
    re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+\b"),  # org/repo#123
    re.compile(r"\bGH-\d+\b", re.IGNORECASE),               # GH-123
    re.compile(rf"\b{_KEY_NUM}\b"),                          # ABC-123 (Linear/Jira/task)
    re.compile(r"\btask\s*[:#]\s*\S+", re.IGNORECASE),      # task:ABC-12 / task #12
    re.compile(r"\bT-\d+\b"),                                # T-12 (short task id)
    re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|ref[s]?)\b[:\s]+{_REF}\b",
        re.IGNORECASE,
    ),  # Closes #12 / Fixes ABC-3 / Refs: T-9  (must point at a real ref)
    re.compile(
        r"https?://\S*(?:github\.com/\S+/issues/\d+|linear\.app/\S+/issue/\S+|"
        r"/browse/[A-Z]+-\d+)",
        re.IGNORECASE,
    ),  # full tracker URLs
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-ticket: {msg}\n")


def has_ticket_reference(text: str) -> bool:
    return any(p.search(text) for p in TICKET_PATTERNS)


def is_exempt(message: str) -> bool:
    """A commit type / marker that doesn't need a ticket (trivial chore, WIP, merge).

    Judged from the first non-empty line of the message (the subject), so a
    `test:`-looking sentence in the body can't accidentally exempt a real change.
    """
    subject = next((ln for ln in message.splitlines() if ln.strip()), "")
    if EXEMPT_MARKERS.search(subject):
        return True
    m = CONVENTIONAL.match(subject)
    return bool(m and m.group(1).lower() in EXEMPT_TYPES)


def commit_message_from_command(command: str, cwd: str | None = None) -> str:
    """Pull the inline commit message out of the argv: -m/--message and -F/--file.

    Returns ONLY the message text — the concatenation of every -m value and the
    contents of any -F file. Deliberately excludes the raw command so that the
    conventional-type / WIP exemption check sees the real subject line, not the
    `git commit …` argv. A relative `-F` path is resolved against `cwd` (the
    command's working directory from the event), since the hook may run elsewhere.
    Best-effort: on unbalanced quotes, falls back to the raw command string
    (better to over-scan for a ticket id than to crash).
    """
    parts: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command  # unbalanced quotes etc. — scan the raw string

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-m", "--message", "-F", "--file") and i + 1 < len(tokens):
            value = tokens[i + 1]
            if tok in ("-F", "--file"):
                parts.append(_read_message_file(value, cwd))
            else:
                parts.append(value)
            i += 2
            continue
        if tok.startswith("--message="):
            parts.append(tok.split("=", 1)[1])
        elif tok.startswith("--file="):
            parts.append(_read_message_file(tok.split("=", 1)[1], cwd))
        elif tok.startswith("-m") and len(tok) > 2:
            parts.append(tok[2:])  # -mMessage
        i += 1
    return "\n".join(parts)


def _read_message_file(path: str, cwd: str | None = None) -> str:
    resolved = os.path.expanduser(path)
    if cwd and not os.path.isabs(resolved):
        resolved = os.path.join(cwd, resolved)
    try:
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        warn(f"could not read commit-message file {resolved}: {exc}")
        return ""


def current_branch(cwd: str | None) -> str:
    """Branch name — a ticket id is often encoded there (feature/ABC-12-foo)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not read branch: {exc}")
        return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # on_error=open → allow on inability to inspect; just warn.
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        return _allow()

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = event.get("cwd") or args.get("cwd")

    if not GIT_COMMIT.search(command) or SKIP_COMMIT.search(command):
        return _allow()  # not a normal authoring commit → nothing to gate

    message = commit_message_from_command(command, cwd)
    # Exemption is judged from the message text only (the real subject line), not
    # the `git commit …` argv — otherwise the command words could mis-trigger it.
    if is_exempt(message):
        return _allow()  # trivial chore / WIP / merge — no ticket expected

    branch = current_branch(cwd)
    # Ticket detection is permissive: scan the message, the raw command (a ticket
    # id may ride in any flag, e.g. a -F path), and the branch name.
    if (
        has_ticket_reference(message)
        or has_ticket_reference(command)
        or has_ticket_reference(branch)
    ):
        return _allow()  # a ticket reference is present → proceed

    advice = (
        "No ticket reference found in this commit message or branch. Non-trivial "
        "changes should start from a ticket (task-cli / GitHub Issue / Linear) with "
        "acceptance criteria, motivation, and user-impact — then reference it in the "
        "commit (e.g. `Refs #123`, `task:ABC-12`, `ENG-456`). If this is a trivial "
        "chore, use a `chore:`/`docs:` type (exempt) or set REQUIRE_TICKET_EXEMPT_TYPES."
    )
    if STRICT:
        emit("block", advice)
        return BLOCK_EXIT_CODE
    # Default: advisory. Surface the reminder but let the commit proceed so the
    # gate can never over-block real work.
    warn(advice)
    emit("allow", advice)
    return 0


def _allow() -> int:
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
