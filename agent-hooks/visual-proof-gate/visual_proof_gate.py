#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require a looked-at screenshot before a UI commit.

Fires on a `git commit`. If the staged change touches USER-VISIBLE files (a component, a
stylesheet, an image, a page/view) it BLOCKs unless a fresh "I looked at a screenshot"
marker exists. It enforces `visual-proof-cycle`: capture the rendered result, read the
capture back, verify it — THEN commit. A "done" claim on a UI change with no screenshot you
actually looked at is the exact failure this gate stops.

What counts as user-visible (staged file inspection):
  - an extension match: .tsx/.jsx/.vue/.svelte/.css/.scss/.less/.html/.svg/.png/.jpg/.jpeg/.gif/.webp
  - OR a path under components/ ui/ pages/ app/ views/ public/ assets/
  If NO user-visible file is staged → allow (nothing to prove).

The marker contract (how it knows a screenshot was looked at):
  The visual-proof-cycle skill / a screenshot-capture step touches a file in:
      ~/.cache/agent-tools/visual-proof/<key>     (mtime = "looked at it" time)
  Any fresh file in that dir (within VISUAL_PROOF_WINDOW_S, default 3600s) satisfies the gate.
  Configure the dir with VISUAL_PROOF_DIR.

This gate straight-BLOCKs (doctrine: "block a commit ... with no attached screenshot"), but
is satisfiable (touch the marker after you VIEW the capture) and escapable.

NOTE: NOT subagent-exempt — a subagent committing UI work must also have looked at the result.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_NO_VISUAL_PROOF=1            — disable the guard for this session
  - env  ALLOW_NO_VISUAL_PROOF_REASON=...   — REQUIRED with the override; logged
  - inline  `# visual-proof-ok: <reason>`   — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the repo cwd in event.cwd
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": process discipline, not a security boundary. The `git diff --cached`
subprocess is timeout-bounded and fails OPEN (if git errors, allow) — a broken stat must
never wedge committing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess  # noqa: S404 — listing staged files is the whole job
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

PROOF_DIR = Path(os.path.expanduser(os.environ.get(
    "VISUAL_PROOF_DIR", "~/.cache/agent-tools/visual-proof")))
PROOF_WINDOW_S = int(os.environ.get("VISUAL_PROOF_WINDOW_S", "3600"))
# Intentionally < the descriptor's `timeout_ms` (8000): the inner python `git diff` timeout
# must fire FIRST and fail OPEN (allow), rather than the bridge killing the whole hook on its
# own timeout. Don't tighten the descriptor to 5000 — the gap is the safety margin (#14).
GIT_DIFF_TIMEOUT_S = 5

# Anchored to a COMMAND invocation (line start, or after a |/&/; separator) with `commit` as
# git's subcommand. Global flags AND their values (`git -C /repo commit`, `git -c k=v commit`)
# are allowed between `git` and `commit`, but the run may NOT cross a command separator — so
# plain text such as `echo "remember to git, then commit"` does NOT trip it (B2).
GIT_COMMIT = re.compile(r"(?:^|[|&;]\s*)git(?:[ \t]+[^\s;&|]+)*?[ \t]+commit\b")
# A rebase/merge plumbing step (`git commit --continue/--abort/--skip`) is not authoring a UI
# change → not gated. This is detected from the PARSED argv (see ``is_skip_commit``), NOT from
# the raw string: a token that only appears in a shell COMMENT (`git commit -m x # --abort`) or
# in the commit MESSAGE (`git commit -m 'support --skip'`) must NOT exempt a real commit.
SKIP_FLAGS = frozenset({"--continue", "--abort", "--skip"})

VISUAL_EXT = re.compile(
    r"\.(?:tsx|jsx|vue|svelte|css|scss|less|html|svg|png|jpg|jpeg|gif|webp)$",
    re.IGNORECASE,
)
VISUAL_DIR = re.compile(r"(?:^|/)(?:components|ui|pages|app|views|public|assets)/", re.IGNORECASE)
INLINE_SENTINEL = re.compile(r"#\s*visual-proof-ok:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"visual-proof-gate: {msg}\n")


def _strip_shell_comment(command: str) -> str:
    """Drop a trailing shell comment (`# …`) that the shell never executes.

    Only an UNQUOTED `#` that starts a word begins a comment, so a `#` inside a quoted commit
    message (`-m 'fix #42'`) or glued to a token (`color=#fff`) is preserved. Best-effort: on a
    tokenization failure (unbalanced quotes) the raw command is returned unchanged."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return command
    return " ".join(tokens)


# SYNC: the commit-segment parser below (_segments / _commit_flags / is_skip_commit) is mirrored
# in skills-read-gate/skills_read_gate.py — each hook is a self-contained standalone script run as
# its own subprocess (no shared import path), so the logic is duplicated by design. Keep both in
# step when changing skip-flag handling.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &)."""
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEP:
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    return segs


def _takes_following_value(tok: str) -> bool:
    """True when a `git commit` flag token consumes the NEXT token as its value.

    Long forms `--message`/`--file` (without `=`); and any short cluster ENDING in `m` or `F`
    (`-m`, `-am`, `-aF`) — the typical `git commit -am 'msg'`, where the message is the following
    token. Stripping it is what stops `git commit -am --skip` (message == a skip flag) from
    falsely reading `--skip` as a continuation flag and exempting a real commit (codex)."""
    if tok.startswith("--"):
        return tok in ("--message", "--file")  # `--message=…`/`--file=…` carry their own value
    if tok.startswith("-") and len(tok) > 1:
        return tok[-1] in ("m", "F")  # short cluster like -m / -am / -aF takes the next token
    return False


def _commit_flags(segment: list[str]) -> list[str] | None:
    """If `segment`'s executable is `git` and its subcommand is `commit`, return the tokens AFTER
    `commit` with message-carrying flags AND their values removed; otherwise None. Walks past git
    GLOBAL options (`-C dir`, `-c k=v`, …) to reach the subcommand."""
    if not segment or segment[0] != "git":
        return None
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2  # global flag + its separate value
            continue
        if tok.startswith("-"):
            i += 1  # other global flag / `-Cdir` / `-ck=v` joined form
            continue
        break
    if i >= len(segment) or segment[i] != "commit":
        return None
    out: list[str] = []
    j = i + 1
    while j < len(segment):
        tok = segment[j]
        if tok == "--":
            break  # everything after `--` is a literal PATHSPEC, never a flag — stop collecting
        if _takes_following_value(tok) and j + 1 < len(segment):
            j += 2  # drop the flag AND its value (-m MSG / -am MSG / --message MSG / -F PATH)
            continue
        if tok.startswith(("--message=", "--file=")) or (tok.startswith("-m") and len(tok) > 2):
            j += 1  # drop -mMSG / --message=MSG / --file=PATH (value glued to the flag)
            continue
        out.append(tok)
        j += 1
    return out


def is_skip_commit(command: str) -> bool:
    """True only when the actual `git commit` SEGMENT carries --continue/--abort/--skip.

    Parses the argv after stripping shell comments, scopes to the `git commit` segment, and
    removes `-m`/`-F` message VALUES — so a skip token that lives only in a comment
    (`git commit -m x # --abort`), in the commit message (`git commit -m 'support --skip'`), or on
    a SIBLING command (`git rebase --abort && git commit -m x`) does NOT exempt an authoring
    commit. On a tokenization failure this returns False → the commit is GATED (the safe way)."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    for seg in _segments(tokens):
        flags = _commit_flags(seg)
        if flags is not None:
            return any(tok in SKIP_FLAGS for tok in flags)
    return False


def staged_files(cwd: str) -> list[str] | None:
    """Names of files staged for commit, or None if git could not be queried (→ fail open)."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv, trusted
            ["git", "-C", cwd, "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"could not list staged files: {exc} — allowing (fail-open)")
        return None
    if proc.returncode != 0:
        warn(f"git diff --cached exited {proc.returncode}: {proc.stderr.strip()} — allowing")
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def visual_staged(files: list[str]) -> list[str]:
    return [f for f in files if VISUAL_EXT.search(f) or VISUAL_DIR.search(f)]


def _proof_fresh() -> bool:
    """True if any marker in PROOF_DIR is fresh (a screenshot was looked at recently)."""
    try:
        if not PROOF_DIR.is_dir():
            return False
        now = time.time()
        for child in PROOF_DIR.iterdir():
            try:
                if (now - child.stat().st_mtime) <= PROOF_WINDOW_S:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_NO_VISUAL_PROOF") == "1":
        reason = (os.environ.get("ALLOW_NO_VISUAL_PROOF_REASON") or "").strip()
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
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = str(event.get("cwd") or os.getcwd())

    # Detect the commit on the comment-stripped command, and judge skip-ness from the parsed
    # argv — so a skip token in a trailing comment / commit message can't bypass the gate.
    if not GIT_COMMIT.search(_strip_shell_comment(command)) or is_skip_commit(command):
        emit("allow")  # not a normal commit → nothing to gate
        return 0

    files = staged_files(cwd)
    if files is None:
        emit("allow")  # git could not be queried → fail open
        return 0

    visual = visual_staged(files)
    if not visual:
        emit("allow")  # no user-visible files staged → nothing to prove
        return 0

    if _proof_fresh():
        emit("allow")  # a screenshot was captured and looked at recently → satisfied
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"visual-proof gate skipped via escape hatch ({reason})")
        emit("allow", f"visual-proof gate skipped via escape hatch ({reason})")
        return 0

    sample = ", ".join(visual[:3]) + (", …" if len(visual) > 3 else "")
    emit(
        "block",
        f"This commit changes user-visible files ({sample}) but no screenshot was captured "
        "and looked at. Per visual-proof-cycle: capture the rendered result, read the capture "
        f"back, verify it, THEN commit. Touch a file under {PROOF_DIR} when you've reviewed a "
        "screenshot, or override with a reason: ALLOW_NO_VISUAL_PROOF=1 + "
        "ALLOW_NO_VISUAL_PROOF_REASON='why', or append `# visual-proof-ok: why`.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
