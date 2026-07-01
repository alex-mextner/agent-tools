#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — stop a SUBAGENT from backgrounding a long process.

The wedge this exists to kill (agent-tools#52, seen ~6x in one session): a dispatched
subagent runs a long process — a multi-model ``review``, a full build/test suite, a
``--watch`` loop, a long ``sleep`` — with ``run_in_background: true`` (or a shell ``&`` /
``setsid``), then ENDS ITS TURN saying "I'll wait for the completion notification". But a
SUBAGENT is NOT re-invoked by a background-completion notification — only the main loop is.
So it idles FOREVER with uncommitted work and no PR, and the orchestrator has to catch the
rest-notification, kill the stray process, and salvage the half-done work by hand.

This gate is the exact INVERSE of two sibling gates:
  - ``no-long-inline-process`` blocks the ORCHESTRATOR from running a long process inline
    and tells it to dispatch the work to a BACKGROUND subagent — it is subagent-EXEMPT.
  - ``background-subagent-gate`` (pre-agent) makes the orchestrator dispatch non-trivial
    subagents in the BACKGROUND.
  - THIS gate makes a SUBAGENT run its OWN long work in the FOREGROUND and block on it: a
    worker must NOT background a long process, because it will never be told it finished.

It fires ONLY for a subagent (``agent_id`` present) AND only when the command is BOTH a
long-running process AND backgrounded. A subagent that runs ``review`` in the foreground
(the correct shape) is allowed; the orchestrator is allowed (its long-process discipline is
``no-long-inline-process``'s job, not this one).

Backgrounding is bound to the long-process command, with correct bash JOB semantics (the wedge
is "the long process ITSELF is detached"), not to the whole line — so ``echo started & review
…`` (the ``echo`` job is backgrounded, ``review`` runs FOREGROUND) is allowed, while ``review …
&`` is blocked. A command counts as backgrounded when:
  - the CC Bash tool's ``run_in_background: true`` flag is set (forwarded into ``args`` by the
    bridge) — it backgrounds the WHOLE command line, so every job is then detached; the
    primary, unambiguous wedge signal;
  - a ``&`` backgrounds the JOB it terminates. ``&`` (like ``;``) launches the entire preceding
    pipeline / AND-OR list, so ``review | tee log &`` and ``review && git commit &`` both
    background ``review`` (the ``&`` is not bound to only the last simple command). ``&&`` is
    logical AND (its own token), and a ``&`` fused into a redirection (``2>&1`` → ``>&``,
    ``&>file``) is a redirect, not a background — both correctly ignored;
  - the command is led by ``setsid`` (genuinely detaches into a new session, returning
    immediately). ``nohup`` alone does NOT background — ``nohup cmd`` runs in the FOREGROUND
    and blocks; the real ``nohup cmd &`` form is caught by the ``&``.
A long process inside a backgrounded SUBSHELL (``(review)&``) is a documented under-block (the
subshell parens end the inner job before the ``&``) — under-block is the safe direction.

What counts as a "long process" is decided by the PARSED command (argv-aware), reusing the
exact detection of ``no-long-inline-process``: the ``review`` CLI as argv[0], a ``--watch``
flag, a build/test suite (npm/pnpm/yarn/bun/deno test|build, pytest/vitest/jest/cypress/
playwright, cargo/go test|build, make/rake/msbuild test|build|all, mvn/gradle
test|build|verify|package), or ``sleep N`` with N>=10s. A keyword inside a quoted argument
to a DIFFERENT command (``tg "…review…" &``) never trips it.

Escape hatch (controllable — mirrors no-long-inline-process):
  - env  ALLOW_SUBAGENT_BACKGROUND=1            — disable the guard for this session
  - env  ALLOW_SUBAGENT_BACKGROUND_REASON=...   — REQUIRED with the override; logged
  - inline  `# subagent-bg-ok: <reason>`         — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the bg flag in
           args.run_in_background, the subagent signal in args.agent_id
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": this is responsiveness/anti-wedge discipline, not a security boundary —
a crash here must never wedge a subagent's ability to run a command. An UNPARSEABLE command
(unbalanced quotes) likewise ALLOWS (fail-open).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

INLINE_SENTINEL = re.compile(r"#\s*subagent-bg-ok:\s*(\S.*)")
# Unescaped shell metachars that, like whitespace, are a word boundary at which a `#` opens a comment
# (`cmd ;# note`, `a |# note`) — used by `_strip_line_comment` to match POSIX comment semantics.
_COMMENT_BOUNDARY_METACHARS = frozenset(";&|()")

# ── what counts as a long-running runner (argv-keyed, not substring) ─────────────────────────
# SYNC: the long-process LABELING surface is duplicated VERBATIM from the canonical source
# agent-hooks/no-long-inline-process/no_long_inline_process.py — the constants below, the
# tokenizer (`_tokenize`/`_strip_*`/`_decompose_punct_token`), the wrapper/env/noise peelers, and
# the label helpers (`_long_process_label`/`_is_suite`/`_sleep_label`/`_is_watch_flag`). Each
# agent-hook is a standalone script run as its own subprocess with no shared import path (see that
# file's tokenization SYNC note), so this parser is duplicated by design — keep it in step with the
# sibling when its detection changes (a drift-guard test compares the constants). NOT copied: the
# BACKGROUND/job segmentation (`_split_jobs`/`_within_job_commands`/`_command_wedge`/`_wedge_label`)
# is NEW here — it implements bash JOB semantics for the `&` background operator, which the sibling
# (an orchestrator foreground-vs-background gate) has no equivalent of.
_DIRECT_RUNNERS = frozenset({"pytest", "vitest", "jest", "cypress", "playwright"})
_TEST_BUILD = re.compile(r"\b(?:test|build)\b")
_TEST_BUILD_ALL = re.compile(r"\b(?:test|build|all)\b")
_TEST_BUILD_VERIFY_PACKAGE = re.compile(r"\b(?:test|build|verify|package)\b")
_SUITE_RUNNERS: dict[str, re.Pattern[str]] = {
    "npm": _TEST_BUILD, "pnpm": _TEST_BUILD, "yarn": _TEST_BUILD, "bun": _TEST_BUILD,
    "deno": _TEST_BUILD, "cargo": _TEST_BUILD, "go": _TEST_BUILD,
    "make": _TEST_BUILD_ALL, "rake": _TEST_BUILD_ALL, "msbuild": _TEST_BUILD_ALL,
    "mvn": _TEST_BUILD_VERIFY_PACKAGE, "gradle": _TEST_BUILD_VERIFY_PACKAGE,
}
_SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SLEEP_OPERAND = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")

_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_SUBSTITUTION_BREAKS = frozenset({"(", ")", "<(", ">("})
_SEGMENT_BREAKS = _SHELL_SEP | _SUBSTITUTION_BREAKS
_PUNCT_CHARS = frozenset("();<>|&")
_BREAKS_LONGEST_FIRST = sorted(_SEGMENT_BREAKS, key=len, reverse=True)
_LEADING_SHELL_NOISE = frozenset({
    "{", "!", "if", "then", "do", "else", "elif", "while", "until",
})
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16
_MAX_LEADING_NOISE = 16

# ── what counts as "backgrounded" (this hook's own signal) ───────────────────────────────────
# A standalone `&` is the shell background operator. `&&` (logical AND) tokenizes to its OWN
# distinct token (never a lone `&`). The trap is REDIRECTIONS: `2>&1`/`>&word`/`&>file` decompose
# to a `&` piece glued to a `>`/`<` — a foreground redirect, NOT a background. So a `&` piece
# counts as background ONLY when it is NOT adjacent to a redirection char (`_REDIR_CHARS`); that
# rejects every redirect form while still catching `&`, `cmd & other`, and a trailing `)&`.
_BACKGROUND_OP = "&"
_REDIR_CHARS = frozenset({">", "<"})
# A `&` backgrounds the whole JOB to its left; these separators END a job WITHOUT backgrounding
# it (the `;`-family list separators + subshell/substitution context boundaries). The within-job
# operators below stay INSIDE the job — `&` backgrounds the entire pipeline / AND-OR list.
_JOB_SEP_NONBG = frozenset({";", ";;", ";&", ";;&", "(", ")", "<(", ">("})
_WITHIN_JOB_OPS = frozenset({"|", "|&", "&&", "||"})
# `setsid` detaches into a new session and the parent returns immediately — a background even
# without a trailing `&`. (`nohup` does NOT: `nohup cmd` runs foreground and blocks.)
_DETACHERS = frozenset({"setsid"})


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"subagent-no-bg-longproc: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    The signal is authoritative ONLY because `cc_hook_bridge` takes agent_id from CC's
    top-level event and DROPS any copy forged in `tool_input` — a worker can't be spoofed in,
    and (the trust direction that matters here) the orchestrator can't be spoofed OUT of the
    gate by a forged `args.agent_id`.
    """
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _is_run_in_background(event: dict) -> bool:
    """The Bash tool's ``run_in_background`` flag, truthily, from ``args`` or the top-level event.

    CC sends a JSON bool under ``args``; we also accept the common stringified / numeric truthy
    forms a wrapping harness might emit (``"true"``/``"1"``/``"yes"``, int/float != 0) so a
    backgrounded long process is never mis-read as foreground. Reads ``args`` first then the
    top-level event — the same fallback used for ``command``/``agent_id`` — so a harness that
    surfaces the flag at the top level isn't missed. Only an explicit truthy value backgrounds,
    so this can't false-positive.
    """
    args = event.get("args") or {}
    val = args.get("run_in_background")
    if val is None:
        val = event.get("run_in_background")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):  # bool handled above → this is a real int/float
        return val != 0
    return isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}


def _inline_sentinel_reason(command: str) -> str | None:
    """The `# subagent-bg-ok:` reason, but ONLY from a REAL shell comment (quote/heredoc-aware).

    A sentinel hidden where bash does NOT see a comment must not override the gate — that would
    be a false ALLOW of a real wedge (the unsafe direction). Three traps the raw-string search
    fell into, all fixed by mirroring the tokenizer's exact line pipeline:
      - inside single-line quotes (`echo "# subagent-bg-ok: x"`) — `#` is not a comment;
      - inside a HEREDOC body (`cat <<EOF` … `# subagent-bg-ok` … `EOF`) — it's data;
      - inside a MULTI-LINE quoted string spanning a newline — the open quote carries over, so a
        `#` on a later line is still quoted.
    So: strip heredoc bodies (`_strip_heredocs`), re-join lines until each chunk tokenizes
    (balancing a multi-line quote, like `_tokenize`), and cut the comment from the BALANCED
    chunk with the quote/escape-aware `_strip_line_comment` (whose state carries across the
    embedded newlines). The sentinel is matched only in that real comment tail.
    """
    joined = command.replace("\r\n", "\n").replace("\r", "\n").replace("\\\n", "")
    lines = _strip_heredocs(joined.split("\n"))
    i = 0
    while i < len(lines):
        chunk = lines[i]
        while _tokenize_line(chunk) is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
        commentless = _strip_line_comment(chunk)
        comment = chunk[len(commentless):]  # the `# …` tail, or "" when the chunk has no comment
        m = INLINE_SENTINEL.search(comment)
        if m:
            return m.group(1).strip()
        i += 1
    return None


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_SUBAGENT_BACKGROUND") == "1":
        reason = (os.environ.get("ALLOW_SUBAGENT_BACKGROUND_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    inline = _inline_sentinel_reason(command)
    if inline:
        return f"inline override: {inline}"
    return None


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes and POSIX backslash-escaping.

    A `#` starts a comment only at a WORD boundary (after whitespace / line start / an
    unescaped metachar) and only OUTSIDE quotes, so a quoted `-m 'fix #42'` keeps its `#`.
    """
    in_single = in_double = False
    prev_ws = True  # start-of-line counts as a word boundary
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and not in_single:
            i += 2  # escaped char: skip it so `\"`/`\\`/`\#` can't toggle quoting or open a comment
            prev_ws = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:i]
        prev_ws = ch.isspace() or (
            ch in _COMMENT_BOUNDARY_METACHARS and not in_single and not in_double
        )
        i += 1
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line (quote-aware comment stripped first); None on unbalanced quotes."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comment already stripped from the raw line (quote-aware)
    try:
        return list(lex)
    except ValueError:
        return None


def _heredoc_terminators(line: str) -> list[tuple[str, bool]]:
    """The here-documents opened on `line` as (terminator-word, dash-form) pairs, FIFO."""
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
    """True when `line` is the here-document terminator (`<<-WORD` strips leading TABS)."""
    candidate = line.lstrip("\t") if dash else line
    return candidate == term


def _strip_heredocs(lines: list[str]) -> list[str]:
    """Drop here-document BODY lines so a body line that LOOKS like a command is not parsed."""
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
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream.

    A NEWLINE is a command separator (a `;` token between lines). Here-document bodies are
    stripped first; a line that fails to tokenize on its own is re-joined with following lines
    until it balances. Returns None only if a chunk can never balance → fail open.
    """
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
            return None  # never balances → fail open
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _decompose_punct_token(tok: str) -> list[str]:
    """Re-split an ALL-punctuation token into known break tokens by greedy longest-match."""
    if not tok or any(ch not in _PUNCT_CHARS for ch in tok):
        return [tok]
    pieces: list[str] = []
    i = 0
    while i < len(tok):
        for br in _BREAKS_LONGEST_FIRST:
            if tok.startswith(br, i):
                pieces.append(br)
                i += len(br)
                break
        else:
            pieces.append(tok[i])  # unknown lone punct char — keep it, it is not a break
            i += 1
    return pieces


def _split_jobs(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Split tokens into shell JOBS (the unit a `&` backgrounds), each tagged backgrounded.

    Correct bash semantics: `&` (like `;` / a newline) is a LIST separator that launches the
    ENTIRE preceding AND-OR-list / pipeline — so `&` backgrounds the whole job to its left, not
    just the last simple command. Thus `review | tee log &` and `review && git commit &` both
    BACKGROUND `review`. The within-job operators `|`, `|&`, `&&`, `||` stay INSIDE the job (a
    later step splits the job into individual commands on them). A `&` adjacent to a redirection
    char (`2>&1` → `>&`, `&>file`) is a redirect, not a terminator. The `;`-family and
    subshell/substitution parens end a job WITHOUT backgrounding it. Returns
    ``[(job_tokens, backgrounded)]``.
    """
    jobs: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    for tok in tokens:
        pieces = _decompose_punct_token(tok)
        for i, piece in enumerate(pieces):
            if piece == _BACKGROUND_OP:
                prev_piece = pieces[i - 1] if i > 0 else None
                next_piece = pieces[i + 1] if i + 1 < len(pieces) else None
                if prev_piece in _REDIR_CHARS or next_piece in _REDIR_CHARS:
                    cur.append(piece)  # a redirection's `&` (`>&`/`&>`), not a job terminator
                    continue
                jobs.append((cur, True))  # `&` backgrounds the WHOLE preceding job
                cur = []
            elif piece in _JOB_SEP_NONBG:
                jobs.append((cur, False))  # `;`-family / subshell boundary ends a job, no bg
                cur = []
            else:
                cur.append(piece)  # words AND within-job ops (`|`/`&&`/…) stay in the job
    jobs.append((cur, False))
    return jobs


def _within_job_commands(job_tokens: list[str]) -> list[list[str]]:
    """Split a job's tokens into the individual commands joined by `|` / `|&` / `&&` / `||`."""
    commands: list[list[str]] = []
    cur: list[str] = []
    for piece in job_tokens:
        if piece in _WITHIN_JOB_OPS:
            commands.append(cur)
            cur = []
        else:
            cur.append(piece)
    commands.append(cur)
    return commands


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading brace-group openers (`{`, `!`) and control keywords (`then`/`do`/…)."""
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _split_inline_env(segment: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off a segment, returning the rest at the executable."""
    i = 0
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/timeout` → `timeout`."""
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading no-op wrapper executables (`timeout 600`, `env CI=1`, `nice -n10`, …)."""
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS and guard < _MAX_WRAPPER_NESTING:
        guard += 1
        wrapper, rest = _basename(argv[0]), argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in _WRAPPER_VALUE_FLAGS:
                    i += 1  # `--signal SIGTERM` / `-k 5` take a separate value token
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional (the wrapped command follows)
            break
        if i:  # consumed the wrapper's own args → real command starts at rest[i:]
            argv = rest[i:]
        elif rest:  # bare wrapper with no args of its own (`time make build`, `nohup cargo build`)
            argv = rest
        else:
            break  # wrapper with nothing after it
    return argv


def _is_suite(exe: str, rest: list[str]) -> bool:
    """True when the segment is a build/test suite (direct runner, or pkg tool + test/build sub)."""
    if exe in _DIRECT_RUNNERS:
        return True
    pat = _SUITE_RUNNERS.get(exe)
    if pat is None:
        return False
    return any(pat.search(tok) for tok in rest)


def _sleep_label(exe: str, rest: list[str]) -> str | None:
    """Label for a long `sleep` (>= 10s) invoked as the command, else None."""
    if exe != "sleep" or not rest:
        return None
    m = _SLEEP_OPERAND.match(rest[0])
    if not m:
        return None
    seconds = float(m.group(1)) * _SLEEP_UNIT_S[m.group(2)]
    if seconds >= 10:
        return f"sleep {m.group(1)}{m.group(2)} (long sleep)"
    return None


def _is_watch_flag(tok: str) -> bool:
    """True for a real `--watch` flag token: `--watch`, `--watch=…`, or `--watch-*`."""
    if any(ch.isspace() for ch in tok):  # de-quoted multi-word message, not a flag token
        return False
    return tok == "--watch" or tok.startswith("--watch=") or tok.startswith("--watch-")


def _long_process_label(argv: list[str]) -> str | None:
    """Return a long-process label for an already-peeled argv (review/--watch/suite/sleep)."""
    if not argv:
        return None
    exe = _basename(argv[0])
    rest = argv[1:]
    if exe == "review":
        return "review (multi-model, minutes-long)"
    if any(_is_watch_flag(tok) for tok in rest):
        return "--watch (a watch loop never exits)"
    if _is_suite(exe, rest):
        return "a build/test suite"
    return _sleep_label(exe, rest)


def _command_wedge(command_tokens: list[str], backgrounded: bool) -> str | None:
    """Return a long-process label when this command is a long process AND detached, else None.

    Detached = the whole job is backgrounded (`backgrounded`) OR the command is led by a
    `setsid` detacher. A foreground command (neither) is the correct shape → None.
    """
    head = _split_inline_env(_strip_leading_shell_noise(command_tokens))
    argv = _strip_wrappers(head)
    label = _long_process_label(argv)
    if label is None:
        return None
    # `setsid` is peeled by `_strip_wrappers`; detect it in the prefix that got peeled so a
    # `setsid` sitting as a non-leading ARGUMENT (`make build setsid`) never counts as a detach.
    peeled_prefix = head[: len(head) - len(argv)]
    setsid_detached = any(_basename(tok) in _DETACHERS for tok in peeled_prefix)
    if backgrounded or setsid_detached:
        return label
    return None


def _wedge_label(tokens: list[str], run_in_background: bool) -> str | None:
    """The label of a BACKGROUNDED long-process command, or None if none is a wedge.

    A `&` backgrounds the whole JOB (pipeline / AND-OR list) to its left, so EVERY command in a
    backgrounded job is detached — `review | tee log &` and `review && git commit &` are both
    flagged. `run_in_background: true` backgrounds the WHOLE command line, so every job is
    detached. LIMITATION (documented under-block, same precision class as the sibling's
    nested-shell limit): a long process wrapped in a NESTED shell construct right before a
    trailing `&` — a subshell `(review)&`, a process substitution `review <(git diff) &`, a
    command substitution `foo $(review) &` — is NOT flagged, because the construct's
    paren/opener is a job boundary that ends the inner job before the `&`. The realistic direct
    wedge forms (`review … &`, `review | tee log &`, `review && commit &`,
    `run_in_background: true`, `setsid review`) are caught; under-block is the safe direction
    for this on_error=open discipline gate.
    """
    for job_tokens, job_bg in _split_jobs(tokens):
        backgrounded = run_in_background or job_bg
        for command_tokens in _within_job_commands(job_tokens):
            label = _command_wedge(command_tokens, backgrounded)
            if label:
                return label
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # This gate governs WORKERS only. The orchestrator's long-process discipline is
    # `no-long-inline-process`'s job; here a non-subagent (no agent_id) is always allowed.
    if not _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    tokens = _tokenize(command)
    if tokens is None:
        # unparseable (unbalanced quotes) → fail open, consistent with on_error=open
        emit("allow")
        return 0

    # The wedge is "a LONG-PROCESS command is itself backgrounded". Backgrounding binds to the
    # whole JOB a `&` launches, so `echo x & review …` (review FOREGROUND) is allowed while
    # `review … &`, `review | tee log &`, and `review && commit &` (review backgrounded) are
    # blocked. A subagent running its long work in the FOREGROUND is the correct shape; a
    # backgrounded SHORT command is no wedge risk.
    matched = _wedge_label(tokens, _is_run_in_background(event))
    if matched is None:
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"subagent background long process allowed via escape hatch ({reason})")
        emit("allow", f"subagent background long process allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        f"You are a SUBAGENT — run this long process ({matched}) in the FOREGROUND and BLOCK "
        "on it; do NOT background it. A subagent is NOT re-invoked by a background-completion "
        "notification "
        "(only the main loop is), so backgrounding this and ending your turn wedges you "
        "FOREVER with uncommitted work and no PR. Remove `run_in_background: true` (and any "
        "trailing `&` / `setsid`) and run it inline so this tool call blocks until it "
        "finishes. Override only with a reason: ALLOW_SUBAGENT_BACKGROUND=1 + "
        "ALLOW_SUBAGENT_BACKGROUND_REASON='why', or append `# subagent-bg-ok: why`.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
