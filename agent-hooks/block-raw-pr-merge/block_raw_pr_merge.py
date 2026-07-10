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

External approval (replaces the OLD self-service escape hatch): there is NO
`ALLOW_RAW_PR_MERGE`(+`_REASON`) env and NO `# no-ship-guard: <reason>` inline sentinel any
more — an agent could set either on its own command, so those merely let the guarded agent
grant itself the bypass this hook exists to stop. The block is now DENY-BY-DEFAULT. Use
`gh ship <PR>` (the green-CI-gated, screenshot-checked path). For a genuine one-time exception,
ASK Alex, or request a single approval by setting
`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<written justification>"`, which routes one Telegram
approval request to Alex (deny-by-default; a bare `1`/`true` is rejected). Only an approved
`tg-ctl ask` (exit 0) allows the raw merge; anything else denies.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a raw merge slipping through a
broken guard is exactly the failure this hook exists to stop.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    module = importlib.util.module_from_spec(spec)
    previous_modules = {
        name: sys.modules[name]
        for name in tuple(sys.modules)
        if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}.")
    }
    for name in previous_modules:
        if name != _HATCH_MODULE:
            sys.modules.pop(name, None)
    sys.modules[_HATCH_MODULE] = module
    # Leave the repo-local module installed on success so later imports in this
    # hook process cannot regain a preloaded user/site package or submodule.
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        for name in tuple(sys.modules):
            if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        # A helper that calls sys.exit() at import must not make the hook exit 0 (allow);
        # convert it to an import failure after cleanup. Ctrl-C still propagates.
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ImportError(f"cannot execute hatch escalation helper from {hatch_init}: {exc}") from exc
    return module


hatch_escalation = _load_hatch_escalation()

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


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


# Separators after which a `#` can begin a comment (an unquoted word boundary).
_SEGMENT_SEPS = frozenset(";&|()")
# Chars that end an UNQUOTED heredoc delimiter word (whitespace or a shell metacharacter).
_HEREDOC_DELIM_END = frozenset(" \t\n;&|()<>")


def _read_heredoc_delimiter(command: str, j: int) -> tuple[str, int] | None:
    """Read ONE shell word starting at `command[j]` and return `(dequoted_word, end_index)`, or None
    on an unterminated quote / empty word. The word is quote-removed exactly as the shell does for a
    heredoc delimiter: `\\EOF`, `E"OF"`, `'EOF'` and `EOF` all yield the terminator `EOF`. Quoting
    the WHOLE word (not just a `'…'`/`"…"` prefix) matters — otherwise `<<\\EOF` / `<<E"OF"` never
    match the unquoted `EOF` terminator, the body skips to end of input, and a real merge after the
    heredoc is ALLOWED (Codex review)."""
    n = len(command)
    out: list[str] = []
    k = j
    while k < n:
        ch = command[k]
        if ch in _HEREDOC_DELIM_END:
            break
        if ch == "\\" and k + 1 < n:
            out.append(command[k + 1])  # backslash-escaped char → literal
            k += 2
            continue
        if ch in ("'", '"'):
            close = command.find(ch, k + 1)
            if close == -1:
                return None  # unterminated quote in the delimiter word → decline (fail toward block)
            out.append(command[k + 1 : close])  # single quotes: verbatim; good enough for a delim
            k = close + 1
            continue
        out.append(ch)
        k += 1
    word = "".join(out)
    return (word, k) if word else None


def _read_heredoc(command: str, i: int) -> tuple[str, bool, int] | None:
    """If `command[i:]` opens a heredoc (`<<WORD` / `<<-WORD`), return `(delimiter, dash,
    index_after_the_operator)`; else None. `<<<` (a here-STRING, one line) is not a heredoc. The
    delimiter is the quote-removed shell word (see `_read_heredoc_delimiter`)."""
    if not command.startswith("<<", i) or command.startswith("<<<", i):
        return None
    n = len(command)
    j = i + 2
    dash = j < n and command[j] == "-"
    if dash:
        j += 1
    while j < n and command[j] in " \t":
        j += 1
    if j >= n:
        return None
    parsed = _read_heredoc_delimiter(command, j)
    if parsed is None:
        return None
    delim, end = parsed
    return delim, dash, end


def _skip_heredoc_bodies(command: str, i: int, delimiters: list[tuple[str, bool]]) -> int:
    """Advance past the bodies of the heredocs opened on the just-ended line. `i` points at the
    first body character (right after the line's newline). For each delimiter in order, consume
    whole lines until a line whose content equals the delimiter (leading tabs ignored when the
    heredoc used `<<-`).

    FAIL-CLOSED: if a terminator line is NOT found ahead, skip NOTHING (return the original `i`).
    That way a `<<` that was NOT really a heredoc opener — arithmetic left-shift `(( a << b ))`, or a
    genuinely unterminated heredoc — does not swallow the rest of the input; the following lines are
    scanned normally, so a real `gh pr merge` after it is still detected (a missed skip only
    over-blocks a body-text mention, the SAFE direction; swallowing would be a BYPASS)."""
    n = len(command)
    pos = i
    for delim, dash in delimiters:
        found = False
        while pos < n:
            nl = command.find("\n", pos)
            line_end = n if nl == -1 else nl
            line = command[pos:line_end]
            pos = line_end + 1 if nl != -1 else n
            if (line.lstrip("\t") if dash else line) == delim:
                found = True
                break
        if not found:
            return i
    return pos


def _normalize_newlines(command: str) -> str:  # noqa: C901 — a small quote/escape/comment scanner
    r"""Remove `\`-newline line continuations (the shell joins the parts with no space) and turn
    each BARE (unquoted) newline into a `;` command separator, quote-, escape-, comment- and
    heredoc-aware.

    In a real shell a bare newline separates commands (like `;`) and a `#` comment runs only to the
    end of its LINE. shlex, fed the whole blob, instead (a) consumes a bare newline as whitespace —
    so `echo ok`+newline+`gh pr merge` collapses into one `echo`-headed segment and the merge on the
    second line evades detection — and (b) runs a `#` comment to the end of the whole INPUT once the
    newlines are gone, so a `# comment` on line 1 would swallow a merge on line 2. This pass fixes
    both BEFORE tokenizing: it drops `#` comments per-line and converts bare newlines to `;`, while
    leaving newlines/`#`/`;` that sit INSIDE quotes literal (so a two-line commit message merely
    mentioning a merge stays one quoted token and is not mis-detected).

    A `#` starts a comment ONLY at an unquoted word boundary (start, or after unescaped whitespace /
    a `;&|()` separator) — tracked by `boundary`, NOT by inspecting the last emitted char, so an
    escaped space (`echo foo\ # x`) keeps the `#` in-word and does not hide a later merge.

    Backslash escaping is honored the way the shell does: an escaped quote can't spoof quote state
    (`echo \"`+newline+`gh pr merge` runs a real merge on line 2 — `\"` is a literal `"`, not an
    opening quote). Outside quotes and inside double quotes a `\` escapes the next char (emitted
    verbatim so shlex, in posix mode, applies the same escape); inside single quotes a `\` is literal.

    Heredoc bodies (`cat <<EOF` … `EOF`) are NOT command lines, so they are skipped whole — else the
    body's newlines would become `;` separators and a `gh pr merge` mentioned in a commit-message
    heredoc would be falsely blocked.
    """
    out: list[str] = []
    quote: str | None = None  # "'" or '"' when inside that quote
    boundary = True  # at an unquoted word boundary (where a `#` may start a comment)
    pending_heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n and quote != "'":  # backslash escape (not in single quotes)
            if command[i + 1] == "\n":  # `\`-newline continuation: REMOVED (parts join, no space)
                i += 2
                continue
            out.append(ch)  # `\<char>`: emit both verbatim; the escaped char is inert and in-word
            out.append(command[i + 1])
            boundary = False
            i += 2
            continue
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            boundary = False
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            boundary = False
        elif ch == "#" and boundary:
            while i < n and command[i] != "\n":  # comment → end of line (drop it)
                i += 1
            continue
        elif ch == "\n":
            # bare newline → command separator. Spaces around the `;` keep shlex from GLUING it to a
            # neighbouring punctuation char into one run — `echo $(true)`+newline+`gh pr merge` would
            # else tokenize as `);` (a run `_split_segments` doesn't treat as a separator), keeping
            # the merge inside the `echo` segment and ALLOWING it (Codex review).
            out.append(" ; ")
            boundary = True
            i += 1
            if pending_heredocs:
                i = _skip_heredoc_bodies(command, i, pending_heredocs)
                pending_heredocs = []
            continue
        elif (heredoc := _read_heredoc(command, i)) is not None:
            delim, dash, i = heredoc
            pending_heredocs.append((delim, dash))
            boundary = False
            continue
        elif ch in " \t":
            out.append(ch)
            boundary = True
        elif ch in _SEGMENT_SEPS:
            out.append(ch)
            boundary = True
        else:
            out.append(ch)
            boundary = False
        i += 1
    return "".join(out)


def _split_segments(command: str) -> list[list[str]]:
    r"""Tokenize `command` via shlex and split into per-segment token lists.

    Uses ``punctuation_chars=True`` so that `;`, `&&`, `||`, `|` etc. are
    returned as standalone separator tokens rather than being glued to adjacent
    words (the default shlex.split() behaviour leaves `done;` as one token,
    missing the `;` separator entirely).

    Each segment is one independent command (the shell atoms between separators).
    Returns a list of token lists; raises ValueError on unbalanced quotes
    (caller treats this as fail-closed).

    `_normalize_newlines` first folds `\`-newline continuations and turns bare newlines into `;`
    (quote- and comment-aware) so a raw merge on a second line cannot evade detection while shell
    comment semantics are preserved.
    """
    command = _normalize_newlines(command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = False
    # `_normalize_newlines` already stripped real (word-boundary, to-end-of-line) `#` comments, so
    # shlex's own commenter must be OFF — otherwise it truncates a MID-WORD `#` (literal in a real
    # shell) to end of input, e.g. `echo foo#bar && gh pr merge 1` would parse as just `['echo',
    # 'foo']` and ALLOW the raw merge. `block-reset-hard` disables it for the same reason.
    lex.commenters = ""
    tokens = list(lex)  # raises ValueError on unbalanced quotes
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPS:
            if current:
                segments.append(current)
                current = []
        elif tok.strip():
            current.append(tok)
        # A pure-whitespace token is never a real argv element. shlex (whitespace_split=False)
        # emits a standalone `'\n'` token for a `\`-newline line continuation; dropping it keeps
        # `VAR=x \`+newline+`gh pr merge` from hiding the merge behind that stray token, which
        # would else strip the `VAR=` assignment, see argv[0] == '\n', and ALLOW the raw merge.
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


def _block(prefix: str | None = None) -> int:
    body = (
        "Refusing a raw `gh pr merge` (incl. --admin): it bypasses the ship gates "
        "(green CI + required screenshots). Use `gh ship <PR>` instead — it merges only "
        "once CI is green and the mandatory screenshots are present. There is NO self-service "
        "override any more (`ALLOW_RAW_PR_MERGE` / `# no-ship-guard:` are gone). For a genuine "
        "one-time exception, ASK Alex, or request a single approval via "
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<why>" — that routes a Telegram approval request '
        "to Alex (deny-by-default; a bare `1` is rejected)."
    )
    emit("block", f"{prefix}\n{body}" if prefix else body)
    return BLOCK_EXIT_CODE


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
    cwd = str(event.get("cwd") or os.getcwd())

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

    # A raw merge is deny-by-default. The ONLY exception is an approved external Telegram hatch
    # (deny-by-default); an unset env falls straight through to the block. The hatch call never
    # raises (it catches internally), so it does not affect this hook's fail-closed contract.
    hatch = hatch_escalation.request_hatch_approval(
        "block-raw-pr-merge",
        {"hook": "block-raw-pr-merge", "command": command},
        cwd=cwd,
        command=command,
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
            return 0
        warn(f"raw `gh pr merge` hatch escalation denied: {hatch.reason}")
        return _block(prefix=f"hatch escalation denied: {hatch.reason}")

    return _block()


if __name__ == "__main__":
    sys.exit(main())
