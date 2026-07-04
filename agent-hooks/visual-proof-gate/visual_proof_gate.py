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
# The separators after which a `cd` runs in the SAME shell process (so its cwd change actually
# persists forward) rather than a subshell — matches require_review.py's validated `_CD_CARRY_SEP`
# model. `&&`/`;` both qualify: the overwhelmingly common, benign pattern IS `setup && cd target
# && git commit` (or `mkdir -p dir && cd dir && ...`), where — if the commit actually runs at
# all — every `&&` link up to it, including the `cd`, necessarily succeeded; treating `&&` as
# untrustworthy here would break that common case for a narrow, deliberately-adversarial
# construction this hook's own doctrine doesn't try to defend against (`on_error: open` —
# process discipline, not a security boundary). `||` is excluded: it runs its right side only
# when the LEFT side FAILED, so a `cd X || next` commit that actually executes means `cd X`
# did NOT succeed — trusting `X` there would be backwards.
_CD_TRUSTED_SEP = frozenset({"&&", ";"})
# The separator immediately AFTER a `cd` that puts the `cd` itself into a subshell/background
# job — `cd /x | next` (pipeline: the left side is a subshell) and `cd /x & next` (backgrounded)
# both mean the `cd`'s cwd change never reaches the shell that runs whatever follows, even
# though the `cd` was otherwise a trusted, leading/`;`-preceded segment. See the cd-trust check.
_CD_SUBSHELL_SEP = frozenset({"|", "&"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &)."""
    return [seg for _sep, seg in _segments_with_preceding_sep(tokens)]


def _segments_with_preceding_sep(tokens: list[str]) -> list[tuple[str | None, list[str]]]:
    """Like `_segments`, but each segment is paired with the separator token immediately
    BEFORE it (`None` for the first segment). `effective_cwd` needs this to tell whether a
    `cd` was unconditionally reached or only gated behind `&&`/`||`/a subshell — see
    `_CD_TRUSTED_SEP` at its call site."""
    out: list[tuple[str | None, list[str]]] = []
    cur: list[str] = []
    prev_sep: str | None = None
    for tok in tokens:
        if tok in _SHELL_SEP:
            out.append((prev_sep, cur))
            prev_sep = tok
            cur = []
        else:
            cur.append(tok)
    out.append((prev_sep, cur))
    return out


def _takes_following_value(tok: str) -> bool:
    """True when a `git commit` flag token consumes the NEXT token as its value.

    Long forms `--message`/`--file`/`--trailer` (without `=`); and any short cluster ENDING in
    `m` or `F` (`-m`, `-am`, `-aF`) — the typical `git commit -am 'msg'`, where the message is
    the following token. Stripping it is what stops `git commit -am --skip` (message == a skip
    flag) from falsely reading `--skip` as a continuation flag and exempting a real commit
    (codex). `--trailer <token>[(=|:)<value>]` is the same shape: per `git commit -h`, the
    whole bracketed value is ONE following token, so `git commit --trailer --skip -m x` must not
    let the trailer's VALUE (`--skip`) leak out and be misread as a real skip flag (codex)."""
    if tok.startswith("--"):
        return tok in ("--message", "--file", "--trailer")  # `=`-glued forms carry their own value
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
        if tok.startswith(("--message=", "--file=", "--trailer=")) or (
            tok.startswith("-m") and len(tok) > 2
        ):
            j += 1  # drop -mMSG / --message=MSG / --file=PATH / --trailer=VAL (glued to the flag)
            continue
        out.append(tok)
        j += 1
    return out


def _git_c_values(segment: list[str]) -> list[str]:
    """Return every `-C <dir>` (or `-C<dir>` joined form) global flag on a git invocation, IN
    ORDER, or [] if there are none. git chains repeated `-C`: each successive one is resolved
    RELATIVE TO THE PREVIOUS one (`git -C a -C b commit` runs in `<cwd>/a/b`, not `<cwd>/b`) —
    so the caller must fold these in sequence through `_resolve_dir`, not just take the last
    raw string (that silently mis-resolved a relative chain, though an absolute last `-C`
    happens to land right either way)."""
    if not segment or segment[0] != "git":
        return []
    found: list[str] = []
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok == "-C" and i + 1 < len(segment):
            found.append(segment[i + 1])
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            found.append(tok[2:])
            i += 1
            continue
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return found


def _cd_target(args: list[str]) -> str | None:
    """Extract the directory word from a `cd`'s argument list (everything after `cd`).

    Skips leading OPTION flags (`-L`, `-P`, `-e`, `-@`) so `cd -P /repo` resolves to `/repo`,
    not a bogus join with the literal flag; `--` ends option parsing so a dir that happens to
    start with `-` after it is still taken literally. `cd -` alone (return to `$OLDPWD`)
    can't be resolved statically — this hook has no memory of a previous directory across
    tool calls — so it returns None (leave `cur` unchanged) rather than guessing wrong."""
    seen_double_dash = False
    for tok in args:
        if not seen_double_dash and tok == "--":
            seen_double_dash = True
            continue
        if not seen_double_dash and tok == "-":
            return None  # `cd -` (OLDPWD) — unresolvable without shell history
        if not seen_double_dash and tok.startswith("-"):
            continue  # an option flag, not the target
        return tok
    return None


_UNEXPANDED_SHELL_META = re.compile(r"[$`*?\[{]")


def _resolve_dir(word: str, cur: str) -> str | None:
    """Resolve one `cd`/`-C` argument against the running `cur` dir, mimicking the shell, or
    None if `word` can't be safely resolved (caller should then leave `cur` unchanged).

    The shell expands a leading `~` or `~user` to a home directory BEFORE the program (cd,
    git) ever sees the word — `cd ~/repo` never receives a literal tilde. Skipping that step
    here left `~` un-expanded and glued onto `cur` as a plain relative path segment
    (`/session/cwd/~/repo`, a directory that doesn't exist), which made the subsequent
    `git -C <bogus path> diff --cached` fail and the whole gate fail OPEN — silently skipping
    the visual-proof check instead of resolving the real target repo. Expand first, THEN
    apply the absolute-vs-relative check so an absolute `~`-expansion short-circuits the join.

    `shlex.split` does NOT perform command substitution (`$(...)`, backticks), variable
    expansion (`$VAR`), or globbing (`*`/`?`/`[.]`/`{...}`) — those are real shell features
    this hook has no shell to run, so a word like `cd "$(git rev-parse --show-toplevel)"` or
    `cd $HOME/proj` arrives here STILL LITERAL. Treating it as a literal path fabricates a
    directory that (almost always) doesn't exist, which — same failure shape as the tilde
    bug above — makes `git -C <bogus> diff --cached` fail and the WHOLE gate fail open. When
    any such metacharacter survives past the `~`-expansion above, this returns None so the
    caller falls back to the current `cur` (the conservative pre-existing behavior) instead
    of fabricating a broken path that defeats the gate entirely."""
    if word.startswith("~"):
        word = os.path.expanduser(word)
    if _UNEXPANDED_SHELL_META.search(word):
        return None
    return word if word.startswith("/") else os.path.normpath(os.path.join(cur, word))


def effective_cwd(command: str, session_cwd: str) -> str:
    """Resolve the ACTUAL directory a `git commit` in `command` runs against.

    A command like `cd /other-repo && git commit ...` or `git -C /other-repo commit ...`
    targets a DIFFERENT repo than the session's own working directory (`event.cwd`) — trusting
    the raw session cwd here would check the wrong repo's staged files entirely (a session
    rooted in repo A committing into repo B via `cd`/`-C` was silently graded against A's
    staged files, producing false positives/negatives unrelated to the actual commit).

    Walks command segments (split on shell separators) left to right, applying each leading
    `cd <dir>` (a real, persistent shell cwd change) UP TO the commit segment, then applies
    that segment's OWN `-C <dir>` if present (git's own precedence: `-C` wins over ambient cwd
    for that one invocation) and stops there. Two things a naive whole-command walk gets
    wrong, both fixed by stopping at the commit segment:
      - a `cd` AFTER the commit (`git commit -m x && cd /elsewhere`) runs only once the commit
        has already happened — it must NOT retroactively change which repo got checked;
      - a `-C` on some OTHER, non-commit git invocation (`git -C /other status && git commit`)
        only scopes that single git call, not the shell's cwd — it must not leak forward onto
        the commit segment's own resolution.
    Best-effort: any parse failure or unrecognized segment shape falls back to `session_cwd`
    unchanged (fail open, matches the rest of this hook's error-handling philosophy). NOTE:
    this can only see a `cd`/`-C` that appears IN this command string — a `cd` issued as a
    separate, earlier shell invocation (persisted shell state across two distinct tool calls)
    is invisible here; that case relies entirely on the caller's `session_cwd` already being
    correct.

    TWO OR MORE commit segments in one command (`git -C /decoy commit -m x && git commit -m
    y`) are AMBIGUOUS: resolving to just the first one's directory would let a harmless-
    looking decoy commit (or a legitimate "commit in repo A, then commit in repo B" chain)
    hide a REAL, unproven commit that lands wherever this call returns nothing for. Rather
    than guess which commit `main()` should actually be gating, this falls back to
    `session_cwd` unchanged whenever there isn't EXACTLY ONE commit segment — the same safe
    default this whole function is a refinement of, restored for the case it can't safely
    resolve on its own. KNOWN GAP (agent-tools#175): this closes the decoy-first bypass but
    does not evaluate every commit segment's OWN target — two real commits into two
    different non-session_cwd repos in one chain still only get session_cwd checked.

    A `cd` is only TRUSTED (applied to `cur`) when it is the first segment or is preceded by
    `&&`/`;` (see `_CD_TRUSTED_SEP`) — the separators where a preceding `cd`'s cwd change
    stays in the SAME shell process as what follows. A `cd` preceded by `||` is only reached
    when an EARLIER command FAILED, so if the commit after it actually runs, that `cd` did
    NOT succeed — trusting its target would be backwards. A `cd` preceded by `|`/`&` runs (if
    at all) in a SUBSHELL whose cwd change never reaches the parent shell.

    The SAME subshell problem applies looking FORWARD, not just backward: `cd /x | git
    commit` and `cd /x & git commit` put the `cd` itself into a subshell/background job (a
    pipeline's left side, or anything before `&`), so even a LEADING `cd` (trusted by the
    preceding-separator check above) never changes the cwd the following `git commit`
    actually runs in — the commit still lands in `session_cwd`. So a `cd` immediately
    FOLLOWED BY `|`/`&` is untrusted too, regardless of what precedes it.

    Trusting an untrusted `cd` anyway would resolve to a real, existing, but IRRELEVANT other
    repo and check it instead of `session_cwd` (where the actual commit lands). So an
    untrusted `cd` (by either check) aborts the whole resolution to `session_cwd`, the same
    as an unresolvable target.

    KNOWN GAP: `&&` is trusted even though, in principle, an adversarial predecessor could be
    CRAFTED to fail on purpose and reach a REAL, existing, but irrelevant repo via a `;` that
    then decouples the eventual commit's execution from whether that `cd` actually ran
    (`false && cd /real-but-irrelevant-repo ; git commit` — a real shell never runs that
    `cd`, so the commit lands in `session_cwd`, but this function would still resolve to the
    other repo). Closing this precisely requires validating the ENTIRE path from a `cd` to
    the commit segment is unbroken `&&` (agent-tools#173), not just the single separator
    immediately around each `cd`. Accepted as a documented, narrow residual gap — this hook's
    own doctrine is `on_error: open` / process discipline, not a security boundary, and the
    common, benign `setup && cd target && git commit` pattern (which trusting `&&` is what
    makes work at all) is far higher value than defending this specific adversarial shape.

    KNOWN GAP (agent-tools#173): tokenization here is plain `shlex.split`, which glues a
    separator with no surrounding whitespace to the adjacent word (`cd /repo;git commit` ->
    one token `/repo;git`, never segmented) — `require_review.py` already solved this with a
    `punctuation_chars`-based tokenizer that this function should eventually adopt too."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return session_cwd

    segments_with_sep = _segments_with_preceding_sep(tokens)
    if sum(1 for _sep, seg in segments_with_sep if seg and _commit_flags(seg) is not None) != 1:
        return session_cwd

    cur = session_cwd
    for i, (sep, seg) in enumerate(segments_with_sep):
        if not seg:
            continue
        if seg[0] == "cd":
            if sep is not None and sep not in _CD_TRUSTED_SEP:
                return session_cwd  # conditionally/subshell-reached `cd` — can't trust it
            following_sep = (
                segments_with_sep[i + 1][0] if i + 1 < len(segments_with_sep) else None
            )
            if following_sep in _CD_SUBSHELL_SEP:
                return session_cwd  # `cd` itself ran (if at all) in a subshell/background
                                     # job — its cwd change never reached the shell that
                                     # runs whatever comes after
            target = _cd_target(seg[1:])
            resolved = _resolve_dir(target, cur) if target is not None else None
            if resolved is None:
                # Regression guard: an unresolvable `cd` (bare `cd -`, or a target this hook
                # can't expand) means we've LOST TRACK of the real shell's cwd from here on
                # — even if an EARLIER `cd` in this same chain resolved cleanly. Leaving
                # `cur` at that earlier, now-stale value (`cd /clean-repo && cd - && git
                # commit`, where the real shell ends up back in session_cwd via $OLDPWD, not
                # /clean-repo) would check a directory the commit never actually targets.
                # Abort to session_cwd — the one directory we can still trust — rather than
                # keep walking on a cwd we're no longer sure of.
                return session_cwd
            cur = resolved
            continue
        if _commit_flags(seg) is not None:
            # Repeated `-C` on the SAME invocation chains: each is resolved relative to the
            # PREVIOUS one (see _git_c_values) — fold them in order, same as the `cd` case.
            # Same staleness guard applies: an unresolvable `-C` value means the invocation's
            # real target can't be trusted either, so abort to session_cwd rather than keep
            # the previous `-C`'s (possibly stale) directory.
            for c_value in _git_c_values(seg):
                resolved = _resolve_dir(c_value, cur)
                if resolved is None:
                    return session_cwd
                cur = resolved
            return cur  # this IS the (one and only) commit — anything after it is irrelevant
    return cur


def is_skip_commit(command: str) -> bool:
    """True only when EVERY `git commit` segment in `command` carries --continue/--abort/--skip.

    Parses the argv after stripping shell comments, scopes to each `git commit` segment, and
    removes `-m`/`-F` message VALUES — so a skip token that lives only in a comment
    (`git commit -m x # --abort`), in the commit message (`git commit -m 'support --skip'`), or on
    a SIBLING command (`git rebase --abort && git commit -m x`) does NOT exempt an authoring
    commit. On a tokenization failure this returns False → the commit is GATED (the safe way).

    Regression this guards against (agent-tools#174): a command chaining a rebase-plumbing
    commit with a REAL one (`git commit --continue && git commit -m x`) used to exempt the
    WHOLE command, because this only inspected the FIRST commit segment found (the plumbing
    one) and returned on it — the second, authoring commit never got checked at all. Requiring
    EVERY commit segment to be a skip closes that: one real commit anywhere in the chain means
    the command is NOT skip-exempt and must be gated normally. NOTE: agent-tools#174's own issue
    body claimed this file was already fixed by agent-tools#172 — that was a misattribution;
    #172 was actually an unrelated `effective_cwd()` tilde-expansion fix, and this file kept the
    original first-segment-only bug live until now. Mirrors the identical, already
    review-approved fix in skills_read_gate.py's `is_skip_commit` — keeps both hooks' skip-flag
    handling in step, per the SYNC comment above."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    commit_segments_flags = [
        flags for seg in _segments(tokens) if (flags := _commit_flags(seg)) is not None
    ]
    if not commit_segments_flags:
        return False
    return all(any(tok in SKIP_FLAGS for tok in flags) for flags in commit_segments_flags)


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


def _resolve_staged_files(cwd: str, session_cwd: str) -> list[str] | None:
    """`staged_files(cwd)`, but falling back to `session_cwd` specifically when `cwd` is a
    `cd`/`-C` target that DOESN'T EXIST ON DISK — the one case where we can be sure a real
    shell's `cd` would not have landed there either (whatever the surrounding operator,
    `cd <bad-dir>` itself fails the same way whether followed by `;`, `&&`, or `||`).
    `effective_cwd` has no shell to run `cd` in and can't know it would have failed, so it
    resolves to the (nonexistent) target anyway; failing open right there would let a real,
    unproven commit in `session_cwd` slip through silently. Falling back to `session_cwd`
    re-checks the one directory we KNOW is real before giving up.

    This does NOT model `&&`/`;`/`||` operator semantics for a cd whose TARGET DOES exist
    (agent-tools#173: e.g. `falsecmd && cd /real-but-irrelevant-repo ; git commit`, where a
    real shell never runs that `cd` at all because `falsecmd` failed) — an existing-but-
    wrong other repo is indistinguishable, from here, from a legitimately reached one, so
    this fallback can't help there. A directory that merely EXISTS but errors on `staged_files`
    for another reason (not a git repo, a transient git error, the subprocess timeout) is
    treated as a genuine 'can't query this repo' situation and stays pure fail-open (allow)
    per the hook's documented policy, rather than substituting an unrelated repo's staged
    files (which could produce an unrelated false BLOCK, or mask the real target's error
    behind an unrelated false ALLOW). A LEGITIMATE `cd <valid-other-repo> && git commit` with
    nothing staged there returns `[]` (not `None`) from `staged_files`, so this fallback
    never fires for it either way."""
    files = staged_files(cwd)
    if files is None and cwd != session_cwd and not os.path.isdir(cwd):
        files = staged_files(session_cwd)
    return files


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
    session_cwd = str(event.get("cwd") or os.getcwd())

    # Detect the commit on the comment-stripped command, and judge skip-ness from the parsed
    # argv — so a skip token in a trailing comment / commit message can't bypass the gate.
    if not GIT_COMMIT.search(_strip_shell_comment(command)) or is_skip_commit(command):
        emit("allow")  # not a normal commit → nothing to gate
        return 0

    # A `cd <other-repo> && git commit` or `git -C <other-repo> commit` targets a DIFFERENT
    # repo than the session's own cwd — check staged files in THAT repo, not session_cwd.
    cwd = effective_cwd(command, session_cwd)

    files = _resolve_staged_files(cwd, session_cwd)
    if files is None:
        emit("allow")  # git could not be queried anywhere → fail open
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
