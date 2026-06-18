#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block pre-commit-gate bypasses (PARSED, not raw-matched).

Denies a shell command that would skip the pre-commit gate:
  - `git commit --no-verify` / `git commit -n`  (the flag in ANY argv position)
  - `git push --no-verify`
  - `git -c core.hooksPath=<...> commit/push`    (a real `-c` config that disables hooks)
  - inline env-var tricks that disable common hook managers (HUSKY=0 / SKIP / LEFTHOOK=0 …)

The decision is made from the PARSED command, NEVER a raw substring of the whole string. This is
the #59 doctrine (visual-proof-gate / skills-read-gate / no-long-inline-process) and #20/#63
(require-review-before-commit): a commit MESSAGE that mentions "--no-verify"/"-n"/"core.hooksPath",
a quoted arg, a shell comment, a HEREDOC body line that looks like a command, or a SIBLING command
in a chain must NOT trip the gate (the old raw regex false-blocked a real agent's `-F` heredoc
message + a `-c core.hooksPath=` arg twice). And a genuine `--no-verify` hidden behind a wrapper
(`timeout … git commit --no-verify`, `sudo git commit --no-verify`) or a fused separator
(`x;git commit --no-verify`) must STILL be caught — the flat regex missed those.

LIMITATION: a commit run through a nested shell-string (`bash -c '…'`/`sh -c '…'`) or `xargs` is not
re-parsed and is therefore not gated — the deliberate precision trade, matching the sibling.

What is flagged, decided per real `git commit`/`git push` SEGMENT (executable + subcommand parsed):
  - a real `--no-verify` FLAG in the subcommand argv (not inside `-m`/`-F`/quoted value, not after
    `--`); plus, for `commit` ONLY, a bare `-n` flag (or a cluster carrying `-n` before its first
    value letter, `-vn`/`-nm`). For `push`, `-n` is `--dry-run`, NOT a bypass — push flags ONLY the
    literal `--no-verify`. A glued message `-m…`/`-F…` and every value-flag's value are peeled first;
  - a real `-c <hook-disabling-config>` git GLOBAL arg (`core.hooksPath=…`).
Plus the inline hook-disabling env (HUSKY=0 / SKIP=… / LEFTHOOK=0 …) parsed off ANY segment,
including an `env HUSKY=0 git commit` wrapper assignment.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (fall back to a few keys)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error for this hook is "closed": a parse failure or crash should DENY, because a bypass that
slipped through a broken gate is exactly what this hook exists to stop. An UNPARSEABLE command
(unbalanced quotes) likewise blocks — the safe default is preserved.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Callable

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Every shell command separator `punctuation_chars=True` can emit as a standalone token, incl. the
# bash compounds `|&` (pipe+stderr) and `;&`/`;;&` (case fall-through). Missing one would weld a
# following `git commit --no-verify` into the previous segment and hide it from the gate.
# SYNC: this tokenizer (_tokenize_line / _tokenize / _segments / _strip_redirects /
# _split_inline_env) is adapted from require-review-before-commit/require_review.py (which adapted
# it from visual-proof-gate / skills-read-gate). Each hook is a self-contained standalone script run
# as its own subprocess (no shared import path), so the parser is duplicated by design. Keep the
# separator/quote/comment handling in step with the siblings when changing it.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Leading tokens that introduce a command but are NOT the command — subshell/brace-group openers and
# the control keywords that precede a command word. A segment may lead with these (a `;` split
# `{ git commit … ; }` leaves `{` first; `if x; then git commit …` leaves `then` first). Stripping
# them recovers the real `git` so a wrapped bypass is still gated (codex finding #2). Trailing
# closers (`)`/`}`/`fi`/`done`) sit on their OWN segment after a separator, so they never lead a
# git segment and need no handling here.
_LEADING_SHELL_NOISE = frozenset({
    "(", "{", "!", "then", "do", "else", "elif", "while", "until", "for", "case",
})
# Shell builtins that SET env vars from their `VAR=value` operands (`export HUSKY=0`, `declare
# LEFTHOOK=0`). A hook-disabling assignment behind one of these persists for the rest of the shell,
# so it must reach the env check — the old raw regex caught `export HUSKY=0` and dropping it would
# be a bypass regression (codex). Their assignments are gathered like an `env` wrapper's.
_ENV_SETTING_BUILTINS = frozenset({"export", "declare", "local", "typeset", "readonly"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# A `VAR=value` token before the executable in a segment is an inline env assignment.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
# Wrapper executables that prefix the REAL command and pass the rest through unchanged. `exec`
# replaces the shell with the command (a transparent prefix), so a bypass behind it must be caught.
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer", "command",
    "sudo", "doas", "exec",
})
# Wrapper flags that take a SEPARATE value, so the NEXT token is the value, not the wrapped cmd:
# `sudo -u USER git …`, `timeout -s SIGTERM …`. Skipping the value keeps the wrapped `git` from
# being misread as the flag's argument. Only flags whose value is a NON-command operand are listed.
_WRAPPER_VALUE_FLAGS = frozenset({"-u", "-g", "-s", "--signal", "-k", "--kill-after"})
# Subcommands of git this gate inspects for a `--no-verify`/`-n` bypass flag.
_GATED_SUBCOMMANDS = frozenset({"commit", "push"})
# A `git -c <key>[=<value>]` config that disables hooks — a genuine bypass vector. `core.hooksPath`
# repoints the hooks dir (e.g. at /dev/null) so no pre-commit gate runs; git treats the key
# case-insensitively, so match it that way. The `=value` is OPTIONAL: a bare `-c core.hooksPath`
# sets the key to boolean "true" (a nonexistent dir → hooks skipped), so it is a bypass too (codex).
_HOOK_DISABLE_CONFIG = re.compile(r"^core\.hookspath(\s*=|$)", re.IGNORECASE)
# Inline env-var tricks that disable common hook managers for one command. Each predicate runs
# against the PARSED env-assignment value (not a raw substring), so words inside a commit message
# can never trip it.
_HOOK_DISABLE_ENV: dict[str, Callable[[str], bool]] = {
    "HUSKY": lambda v: v == "0",
    "LEFTHOOK": lambda v: v == "0",
    "SKIP": lambda v: bool(v),
    "PRE_COMMIT_ALLOW_NO_CONFIG": lambda v: bool(v),
    "GIT_HOOKS_SKIP": lambda v: bool(v),
}


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-no-verify: {msg}\n")


# ── tokenization (mirrors the #59 sibling parser) ────────────────────────────────────────────

def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes (so the `#` is found in the RAW
    text, BEFORE shlex de-quotes). A `#` starts a comment only at a WORD boundary — preceded by
    whitespace or at the start of the line — and only OUTSIDE single/double quotes. So a quoted
    message that STARTS with `#` (`-m '#wip'`) or contains one (`-m 'fix #42'`) keeps its `#` and is
    NOT truncated. Doing this on the de-quoted TOKEN (the old way) wrongly treated a quoted `#wip`
    as a comment — dropping the token AND any real `--no-verify` after it (codex HIGH finding)."""
    in_single = in_double = False
    prev_ws = True  # start-of-line counts as a word boundary
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:idx]
        prev_ws = ch.isspace()
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line: drop a word-boundary `#` comment (quote-aware, see
    ``_strip_line_comment``) then split GLUED separators (`x;git`, `a&&git`) into standalone tokens.
    `punctuation_chars=True` emits `; & | && ||` as their own tokens while honoring quotes. Returns
    None on unbalanced quotes → the caller fails safe."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comment already stripped from the raw line (quote-aware)
    try:
        return list(lex)
    except ValueError:
        return None


def _heredoc_terminators(line: str) -> list[tuple[str, bool]]:
    """The here-documents opened on `line` as (terminator-word, dash-form) pairs, FIFO; [] if none /
    untokenizable. Detection runs on the TOKENIZED line so a `<<WORD` INSIDE a quoted string
    (`echo "see <<NOTE"`) is absorbed into the quote and is NOT a false opener (codex finding #2):
    with `punctuation_chars=True`, shlex emits a real `<<` as its own token and the next token is the
    terminator. `dash-form` is True for `<<-WORD` (leading TABS allowed on the terminator line)."""
    toks = _tokenize_line(line)
    if toks is None:
        return []
    terms: list[tuple[str, bool]] = []
    for idx, tok in enumerate(toks):
        if tok == "<<" and idx + 1 < len(toks):
            raw = toks[idx + 1]
            dash = raw.startswith("-")
            word = raw.lstrip("-") if dash else raw
            if word:
                terms.append((word, dash))
    return terms


def _heredoc_body_ended(line: str, term: str, dash: bool) -> bool:
    """True when `line` is the here-document terminator. POSIX: a plain `<<WORD` needs the terminator
    line to match EXACTLY (no leading whitespace); only `<<-WORD` strips leading TABS (codex finding
    #4). A trailing newline was already removed by the line split."""
    candidate = line.lstrip("\t") if dash else line
    return candidate == term


def _strip_heredocs(lines: list[str]) -> list[str]:
    """Drop here-document BODY lines (the data between `<<WORD` and its terminator) so they are not
    parsed as commands. The opener LINE is kept (it carries the real `git commit -F -`); only the
    body + terminator lines are removed. Multiple heredocs opened on one line are consumed FIFO
    (`cmd <<A <<B` → body-A then body-B). An unterminated heredoc consumes to EOF — fine, the body
    is data either way. Quote-aware via ``_heredoc_terminators`` so a quoted `<<X` never opens a
    spurious heredoc that would swallow a following REAL command (codex finding #2)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        terminators = _heredoc_terminators(line)
        i += 1
        for term, dash in terminators:
            while i < len(lines) and not _heredoc_body_ended(lines[i], term, dash):
                i += 1  # a body (data) line — drop it
            if i < len(lines):
                i += 1  # drop the terminator line too
    return out


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a NEWLINE
    is a command separator (a `;` token between lines). Here-document bodies are stripped first (see
    ``_strip_heredocs``) so a body line that LOOKS like a command (`git commit --no-verify` inside an
    `-F -` heredoc) is data, not a segment. A remaining line that fails to tokenize on its own (a
    quoted string spanning a newline) is re-joined with following lines until it balances. Returns
    None only if a chunk can never be balanced → the caller fails safe."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")  # honor backslash-newline line continuations
    lines = _strip_heredocs(joined.split("\n"))
    out: list[str] = []
    first = True
    i = 0
    while i < len(lines):
        chunk = lines[i]
        toks = _tokenize_line(chunk)
        while toks is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
            toks = _tokenize_line(chunk)
        if toks is None:
            return None  # never balances → fail safe
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &, fused forms)."""
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


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading subshell/brace openers (`(`, `{`, `!`) and control keywords (`then`/`do`/`else`/
    `elif`) so a command introduced by them is recovered: `(git commit --no-verify)` → `git commit
    --no-verify`, `if x; then git commit --no-verify` → (the `then` segment) `git commit …` (codex
    finding #2). Capped so a pathological all-noise segment can't loop."""
    i = 0
    while i < len(segment) and i < 16 and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


# Flags whose VALUE could itself be a pure `<`/`>` token (a quoted `-m '<'`). After posix shlex,
# quotes are gone, so such a value is indistinguishable from a redirect operator by shape alone —
# but it CANNOT be a redirect because it fills the preceding flag's value slot. So a `<>&`-only token
# right after one of these is its value, not a redirect (codex finding #2). Conservatively broad:
# every value-taking flag of commit/push (separate or message-bearing short cluster).
_VALUE_FLAG_BEFORE_REDIRECT = frozenset({
    "-m", "--message", "-F", "--file", "--author", "--date", "-C", "--reuse-message",
    "-c", "--reedit-message", "--fixup", "--squash", "--cleanup", "-t", "--template", "--trailer",
    "-o", "--push-option", "--receive-pack", "--exec", "--repo",
})


def _is_value_flag_before_redirect(tok: str) -> bool:
    """True when `tok` is a flag whose following token is its VALUE (so a `<`/`>`-shaped next token is
    data, not a redirect). Covers separate value-flags AND message-bearing short clusters (`-am`)."""
    if tok in _VALUE_FLAG_BEFORE_REDIRECT:
        return True
    return (tok.startswith("-") and not tok.startswith("--") and len(tok) > 1
            and tok[-1] in ("m", "F", "C") and "=" not in tok)  # cluster like `-am` / `-aF`


def _strip_redirects(segment: list[str]) -> list[str]:
    """Drop shell redirection operators + their targets from a segment so they don't leak into argv.
    A pure `<>&` token is a redirect operator; a `<`/`>` INSIDE a quoted word stays a normal token.
    A `<>&`-only token directly after a value-flag (`-m '<'`) is that flag's VALUE, not a redirect
    (codex finding #2). After `--` everything is a literal pathspec (never a redirect)."""
    out: list[str] = []
    i = 0
    seen_ddash = False
    while i < len(segment):
        tok = segment[i]
        prev = segment[i - 1] if i > 0 else ""
        is_redir = (not seen_ddash and bool(tok) and tok not in _SHELL_SEP
                    and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok)
                    and not _is_value_flag_before_redirect(prev))
        if is_redir:
            # A bare digit right before the redirect is an fd (`2` of `2> err`) — UNLESS it is the
            # value of a value-flag (`-m 2 > log`), where `2` is the message, not an fd (codex). Only
            # pop a true fd digit so the value-flag's value stays put and consumes correctly later.
            before_digit = out[-2] if len(out) >= 2 else ""
            if out and out[-1].isdigit() and not _is_value_flag_before_redirect(before_digit):
                out.pop()
            i += 2 if i + 1 < len(segment) else 1  # drop the operator AND its target
            continue
        if tok == "--":
            seen_ddash = True
        out.append(tok)
        i += 1
    return out


def _split_inline_env(segment: list[str]) -> tuple[dict[str, str], list[str]]:
    """Peel leading `VAR=value` assignments off a segment → (env, rest-starting-at-executable)."""
    env: dict[str, str] = {}
    i = 0
    while i < len(segment):
        m = _INLINE_ENV.match(segment[i])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        i += 1
    return env, segment[i:]


# ── wrapper peeling + git-segment classification ─────────────────────────────────────────────

def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/git` → `git`, `./sudo` → `sudo`. Used
    so a path-qualified wrapper/git is recognized the same as the bare name (codex regression)."""
    return tok.rsplit("/", 1)[-1]


def _is_git_executable(tok: str) -> bool:
    """True when `tok` is the git binary — bare `git` or a path to it (`/usr/bin/git`, `./git`)."""
    return _basename(tok) == "git"


def _strip_wrappers(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """Peel leading wrapper executables (`timeout 60`, `env A=b`, `nice -n 10`, `stdbuf -oL`, …) so
    the REAL command beneath is what we inspect, returning (env, rest). A path-qualified wrapper
    (`/usr/bin/sudo`) is matched by basename, like git (codex regression). A wrapper's own option
    flags and its positional operand (`timeout`'s duration, `nice`'s priority) are skipped past; for
    `env`, the `VAR=value` assignments are COLLECTED into `env` (an `env HUSKY=0 git …` is a real
    hook-disabling bypass — so the assignments must reach the env check, not be discarded). Stops as
    soon as the head is no longer a known wrapper, so a real `git` is never skipped over."""
    env: dict[str, str] = {}
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS and guard < 16:
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        if wrapper == "env":
            assigned, argv = _split_inline_env(argv)  # `env [-i] VAR=val … cmd` → collect VAR=val
            env.update(assigned)
    return env, argv


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    """Drop one wrapper's own option flags + (for timeout/nice/ionice) its leading numeric operand,
    returning argv positioned at the wrapped command (or at the wrapper's `VAR=val` env, for `env`).
    A flag in ``_WRAPPER_VALUE_FLAGS`` (`sudo -u alice`, `timeout -s TERM`) consumes the following
    token as its value. Conservative: a token already recognizable as git ends the skip immediately,
    so the wrapped `git` is never consumed."""
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i] in _WRAPPER_VALUE_FLAGS and i + 1 < len(argv) and not _is_git_executable(
                argv[i + 1]):
            i += 2  # the flag + its separate value (`-u alice`, `-s SIGTERM`)
            continue
        i += 1  # `-n`, `--signal=SIGTERM`, `-oL`, `-i`, `-E` … (the wrapper's own flag)
    if i < len(argv) and argv[i] == "--":
        i += 1
    if (wrapper in ("timeout", "nice", "ionice")
            and i < len(argv) and not _is_git_executable(argv[i])):
        i += 1  # drop the duration (`60`, `1m`) / priority operand
    return argv[i:]


def _git_subcommand_argv(argv: list[str]) -> tuple[str, list[str], list[str]] | None:
    """If `argv` (wrappers already peeled) runs `git commit`/`git push`, return (subcommand,
    global_args, subcommand_argv); else None. `global_args` are the tokens between `git` and the
    subcommand (where a `-c core.hooksPath=…` bypass lives); `subcommand_argv` are the tokens AFTER
    the subcommand. Walks past git GLOBAL options to reach the subcommand."""
    if not argv or not _is_git_executable(argv[0]):
        return None
    global_args: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(argv):
            global_args.extend(argv[i:i + 2])  # global flag + its separate value
            i += 2
            continue
        if tok.startswith("-"):
            global_args.append(tok)  # other global flag / glued `-Cdir` / `-ck=v`
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] not in _GATED_SUBCOMMANDS:
        return None
    return argv[i], global_args, argv[i + 1:]


# ── bypass detection on the parsed git segment ───────────────────────────────────────────────

# Per-subcommand LONG flags whose value is a REQUIRED separate token (or glued via `=`). A
# `--no-verify` sitting in that value (`commit --author '--no-verify'`, `push --push-option
# '--no-verify'`) must never be read as the real bypass flag. EXCLUDES flags with an OPTIONAL value
# (`--gpg-sign`, which only ever carries a GLUED `=keyid`): treating those as next-token-consuming
# would let `git commit --gpg-sign --no-verify` swallow the real `--no-verify` (codex finding #1).
_LONG_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "commit": frozenset({
        "--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message",
        "--fixup", "--squash", "--cleanup", "--template", "--trailer",
    }),
    # `git push` value-flags: `--push-option=…`/`-o …` carries an arbitrary string (which could be
    # `--no-verify`), plus `--receive-pack`/`--repo`/`--exec` (codex finding #4).
    "push": frozenset({"--push-option", "--receive-pack", "--exec", "--repo"}),
}
# Per-subcommand SHORT flags whose letter consumes a REQUIRED value. For `commit` (`-m`/`-F`/`-C`/
# `-c`/`-t`): in a CLUSTER the FIRST such letter takes the rest-of-cluster (or next token) as its
# value, so a later `n` is part of that value, not a `--no-verify`; an `n` BEFORE the letter is the
# real flag. `-S` (gpg-sign) is EXCLUDED — its value is OPTIONAL and only glued (`-Skeyid`), never
# the next token, so `git commit -S --no-verify` must NOT swallow the `--no-verify` (codex finding
# #1). For `push`, only `-o` (`--push-option`) takes a value; push has no `-n`-as-no-verify (bare
# `-n` is `--dry-run`), so push never inspects a cluster for `n`.
_SHORT_VALUE_LETTERS: dict[str, frozenset[str]] = {
    "commit": frozenset("mFCct"),
    "push": frozenset("o"),
}
# SHORT flags with an OPTIONAL glued value (`-S`/`-Skeyid`). They never consume the NEXT token (so
# they are NOT in `_SHORT_VALUE_LETTERS`), but within a cluster their GLUED tail is a value — so a
# later letter in that tail is data, not a flag. `_short_cluster_has_n` must stop at them too, else
# a keyid containing `n` (`-Sname@example`) would be a false `--no-verify` (codex finding #3).
_SHORT_OPTIONAL_VALUE_LETTERS: dict[str, frozenset[str]] = {
    "commit": frozenset("S"),
    "push": frozenset(),
}
# The shortest UNAMBIGUOUS abbreviation of `--no-verify` that git resolves to it. Git accepts any
# unambiguous prefix of a long option, so `git commit --no-veri` runs `--no-verify`; `--no-ver` and
# shorter are AMBIGUOUS (git rejects them: `--no-verbose` shares the prefix), so they are NOT a
# bypass. `--no-veri`/`--no-verif`/`--no-verify` all execute the bypass (codex finding #1).
_NO_VERIFY_FLAG = "--no-verify"
_NO_VERIFY_MIN_PREFIX = "--no-veri"


def _is_no_verify_flag(tok: str) -> bool:
    """True when `tok` is `--no-verify` or an UNAMBIGUOUS abbreviation git resolves to it
    (`--no-veri`/`--no-verif`). Excludes the ambiguous `--no-ver`/`--no-ve`/`--no-v` (which git
    rejects) and any glued `=value` form (`--no-verify` takes no value)."""
    return (
        tok.startswith(_NO_VERIFY_MIN_PREFIX)
        and _NO_VERIFY_FLAG.startswith(tok)
        and len(tok) <= len(_NO_VERIFY_FLAG)
    )


def _has_no_verify_flag(subcommand: str, argv: list[str]) -> bool:
    """True when a REAL `--no-verify` bypass flag is present in the subcommand argv. Stops at `--`
    (the rest are literal pathspecs); skips the VALUE of every value-flag (so `--author '--no-verify'`
    / `-m '... -n'` / `push -o '--no-verify'` is not a hit).

    The `-n` short form is subcommand-DEPENDENT (codex finding #1): for `git commit`, a `-n` flag
    (bare, or in a cluster BEFORE the first value letter, `-vn`/`-nm`) IS `--no-verify`; for `git
    push`, `-n` is `--dry-run`, NOT a bypass — so for `push` ONLY the literal `--no-verify` long flag
    counts. The walk consumes value tokens itself, so a single left-to-right pass stays correct for
    clusters like `-nm wip` (where pre-stripping would wrongly drop the `n`)."""
    is_commit = subcommand == "commit"
    long_value = _LONG_VALUE_FLAGS.get(subcommand, frozenset())
    short_value = _SHORT_VALUE_LETTERS.get(subcommand, frozenset())
    # A cluster's `n` is only a real flag BEFORE the first value-taking letter — required OR optional
    # (`-S`'s glued keyid). The `has_n` scan stops at either kind; only the required set drives
    # next-token consumption below.
    n_stop = short_value | _SHORT_OPTIONAL_VALUE_LETTERS.get(subcommand, frozenset())
    j = 0
    while j < len(argv):
        tok = argv[j]
        if tok == "--":
            return False  # the rest are literal pathspecs
        if _is_no_verify_flag(tok):
            return True  # `--no-verify` or an unambiguous abbreviation (`--no-veri`, `--no-verif`)
        if tok.startswith("--"):
            if "=" not in tok and tok in long_value and j + 1 < len(argv):
                j += 2  # `--author X` / `--push-option X` — skip the flag AND its separate value
                continue
            j += 1  # `--amend`, `--author=X` (glued value), other long flag — not no-verify
            continue
        if tok.startswith("-") and len(tok) > 1:
            # An `n` BEFORE the cluster's first value letter is a real `--no-verify` even when the
            # cluster also consumes a value (`-nm wip`: `-n` flag, then `-m` takes "wip").
            if is_commit and _short_cluster_has_n(tok, n_stop):
                return True
            if _short_cluster_takes_next_value(tok, short_value) and j + 1 < len(argv):
                j += 2  # a cluster like `-am` / `-aF` / push `-o` consumes the NEXT token as value
                continue
            j += 1
            continue
        j += 1  # a positional (pathspec / value already consumed)
    return False


def _short_cluster_takes_next_value(tok: str, value_letters: frozenset[str]) -> bool:
    """True when a short cluster's value letter is its LAST char (so the value is the NEXT token):
    `-m`, `-am`, `-aF`, push `-o`. A glued value (`-mMSG`, where the letter is NOT last) carries its
    value inside the token and does not consume the next one. Empty `value_letters` (a subcommand
    with no value-taking short flag) → always False."""
    body = tok[1:]
    return bool(body) and body[-1] in value_letters and not _glued_short_value(tok, value_letters)


def _glued_short_value(tok: str, value_letters: frozenset[str]) -> bool:
    """True when a short cluster carries a GLUED value: a value letter appears with chars AFTER it
    (`-mMSG`, `-FPATH`, `-amMSG`). Those trailing chars are the value, not flags, so the cluster
    neither consumes the next token nor exposes a later `n` as a real flag."""
    body = tok[1:]
    for idx, ch in enumerate(body):
        if ch in value_letters:
            return idx < len(body) - 1  # a value letter with something glued after it
    return False


def _short_cluster_has_n(tok: str, value_letters: frozenset[str]) -> bool:
    """True for a short-flag cluster that carries a real `-n` (`-n`, `-vn`, `-nv`, `-nm`). A git short
    cluster is read left-to-right: the FIRST value letter (`m`/`F`/`C`/…) consumes the REST of the
    cluster as its value, so `n` is a real flag only when it appears BEFORE that letter: `-nm` → `n`
    flag then `m`-message (BLOCK); `-mn` → `m` takes "n" as its glued message (ALLOW)."""
    for ch in tok[1:]:
        if ch == "n":
            return True  # `n` reached before any value letter — a real `--no-verify`
        if ch in value_letters:
            return False  # a value letter consumes the rest of the cluster; a later `n` is value
    return False


def _hook_disable_config(global_args: list[str]) -> str | None:
    """The hook-disabling `git -c <key>=<value>` config in the global args, or None. Matches a
    separate `-c core.hooksPath=…` and a glued `-ccore.hooksPath=…`."""
    i = 0
    while i < len(global_args):
        tok = global_args[i]
        if tok == "-c" and i + 1 < len(global_args):
            if _HOOK_DISABLE_CONFIG.match(global_args[i + 1]):
                return global_args[i + 1]
            i += 2
            continue
        if tok.startswith("-c") and len(tok) > 2 and _HOOK_DISABLE_CONFIG.match(tok[2:]):
            return tok[2:]  # glued `-ccore.hooksPath=…`
        i += 1
    return None


def _hook_disable_env(env: dict[str, str]) -> str | None:
    """The first inline env assignment that disables a hook manager, formatted `KEY=value`, or None.
    Decided from the PARSED env (e.g. `HUSKY=0`), never a raw substring of the command."""
    for key, value in env.items():
        pred = _HOOK_DISABLE_ENV.get(key)
        if pred is not None and pred(value):
            return f"{key}={value}"
    return None


def _export_env(argv: list[str]) -> dict[str, str]:
    """The `VAR=value` assignments of an env-setting builtin segment (`export HUSKY=0`, `declare
    LEFTHOOK=0`), or {} when the segment is not one. `export HUSKY=0; git commit` disables hooks for
    the rest of the shell, so the assignment must reach the env check — the old raw regex caught it
    (codex). Only literal `VAR=value` operands are collected; a bare `export HUSKY` (no value) sets
    nothing here and is ignored."""
    if not argv or argv[0] not in _ENV_SETTING_BUILTINS:
        return {}
    env: dict[str, str] = {}
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue  # an option to the builtin (`export -p`, `declare -x`)
        m = _INLINE_ENV.match(tok)
        if m:
            env[m.group(1)] = m.group(2)
    return env


# ── verdict ──────────────────────────────────────────────────────────────────────────────────

_NO_VERIFY_MSG = (
    "Refusing `git commit/push --no-verify` (-n): the pre-commit gate "
    "(lint/typecheck/tests) must not be bypassed. Fix the failing check "
    "instead of skipping the hook."
)
_HOOK_PATH_MSG = (
    "Refusing `git -c core.hooksPath=…` on a commit/push: repointing the hooks dir "
    "disables the pre-commit gate. Fix the failing check instead of disabling hooks."
)
_HOOK_ENV_MSG = (
    "Refusing to run with hooks disabled (HUSKY=0/SKIP/LEFTHOOK=0/...): the "
    "pre-commit gate must not be bypassed. Fix the failing check instead."
)


def find_bypass(command: str) -> str | None:
    """Inspect a PARSED command and return a human message for the FIRST gate-bypass found, or None
    if the command is clean. Raises ValueError on an unparseable command so ``main`` can fail
    closed."""
    tokens = _tokenize(command)
    if tokens is None:
        raise ValueError("unbalanced quotes")
    for raw_segment in _segments(tokens):
        segment = _strip_leading_shell_noise(_strip_redirects(raw_segment))
        seg_env, rest = _split_inline_env(segment)
        wrapper_env, after_wrappers = _strip_wrappers(rest)
        # A wrapper like `sudo` accepts `VAR=value` operands of its OWN (`sudo FOO=bar git …`); peel
        # any that survive the wrapper strip so both the leftover env and the real `git` are seen
        # (codex). Without this, `sudo HUSKY=0 git commit --no-verify` would slip — argv[0] would be
        # `HUSKY=0`, not `git`.
        post_env, argv = _split_inline_env(after_wrappers)
        # The hook-disabling env check runs on EVERY segment — inline (`HUSKY=0 make`), wrapper-
        # collected (`env HUSKY=0 make`), post-wrapper (`sudo HUSKY=0 git …`), AND an env-setting
        # builtin (`export HUSKY=0`) — regardless of whether the command is git, so all forms behave
        # the same. The merge prefers later values.
        if _hook_disable_env({**seg_env, **wrapper_env, **post_env, **_export_env(argv)}):
            return _HOOK_ENV_MSG
        parsed = _git_subcommand_argv(argv)
        if parsed is None:
            continue
        subcommand, global_args, subcommand_argv = parsed
        if _hook_disable_config(global_args):
            return _HOOK_PATH_MSG
        if _has_no_verify_flag(subcommand, subcommand_argv):
            return _NO_VERIFY_MSG
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-no-verify: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    try:
        message = find_bypass(command)
    except Exception as exc:  # noqa: BLE001 — fail-closed: any inspection failure DENIES
        warn(f"could not inspect the command: {exc} — blocking (fail-closed)")
        emit("block", "block-no-verify: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    if message:
        emit("block", message)
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
