#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a PATTERN-based kill of a SHARED process name.

Denies a shell command that kills processes by NAME/PATTERN match (`pkill -f node`, `killall
codex`, `kill $(pgrep -f "review diff")`, `pgrep -f playwright | xargs kill`) when the pattern
being matched is a generic, widely-shared tool/process name — because `pkill -f`/`killall`/
`pgrep` match against EVERY process on the machine whose command line contains the pattern,
not just the caller's own. Two real incidents motivate this (2026-07-01 agent-ecosystem
retrospective, gap G-5): a subagent's `pkill -f "review diff"` killed a DIFFERENT session's
in-flight code review (2026-06-26), and a narrow-grep-based kill nearly killed another
session's e2e matrix run (2026-06-27). Both were accidental collateral damage, not deliberate
bypasses — this hook exists to turn "kill everything matching this word" into a deliberate,
scoped, or explicitly-approved action.

Detection is ARGV-BASED (shlex), not a raw substring match (same discipline as
block-raw-pr-merge / block-reset-hard / block-no-verify). Three shapes are covered:

  1. `pkill [flags] <pattern>` / `killall [flags] <name>...` — the pattern/name argument(s).
  2. `kill $(pgrep ...)` / `` kill `pgrep ...` `` — a PID list sourced from a `pgrep` command
     substitution; the substitution's own pattern is extracted and classified.
  3. A pipeline that resolves PIDs by pattern and feeds them to `kill`: `pgrep <pattern> |
     xargs kill`, or the "narrow grep" shape `ps aux | grep <pattern> | ... | xargs kill`.

A pattern is DANGEROUS (and requires approval) only when it is — or contains, as a whole
word/phrase — a known SHARED tool/process name (`_SHARED_PROCESS_NAMES` below) AND it carries
no session-scoping signal (`_looks_session_scoped`: a path, a hex/uuid-looking run, or a long
digit run — the shape of a worktree path, a harness user-data-dir prefix, a port, or a PID).
This is deliberately a DENYLIST of known-ambiguous names, not a blanket "block every pattern
kill" — the sanctioned e2e-harness recipe `pkill -9 -f "<user-data-dir-prefix>"` (repo
CLAUDE.md) and any `kill <pid>` must keep working with zero friction; only the actually
collateral-prone shape (a bare shared tool name with nothing scoping it to the caller's own
process) is gated.

Allowed (let through, no approval needed):
  - `kill <pid> [<pid> ...]` (optionally with a `-SIGNAL`/`-s SIGNAL` flag) — always
    PID-targeted, cannot hit another session's process by name collision.
  - A pattern carrying a session-scoping signal even if it also mentions a shared tool name
    (`pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/node_modules/.bin/vitest"`,
    `pkill -9 -f "hvsc-3-a1b2c3d4"`) — the match set is already narrow.
  - An unrecognized pattern not on the denylist — fails OPEN on unknowns so this hook never
    blocks a legitimate, project-specific kill it has no reason to distrust; it exists to gate
    KNOWN-ambiguous names, not to police every `pkill` invocation.
  - `dev stop` (the project's own safe, repo-scoped dev-process stop command) is never touched
    by this hook (it does not invoke pkill/killall/kill itself).

External approval (deny-by-default, no self-service bypass): request a one-time Telegram
approval by setting `RIG_HATCH_REQUEST_PKILL_GUARD="<written justification>"` — routes through
the shared `agenttools_hatch_escalation` helper (the same mechanism as background-subagent-gate
/ block-raw-pr-merge), auto-recorded in `overrides.log` (G-8). A blank/bare-flag value is
rejected. Nothing set = denied; use a PID-targeted `kill <pid>` or `dev stop` instead.

Known limitations (documented, not silently missed — same bar as sibling hooks: would a
confused, non-evasive agent produce this BY ACCIDENT?):
  - The denylist is necessarily incomplete; an unlisted shared name is not caught (fails open
    toward not blocking legitimate work — see "Allowed" above).
  - Only ONE level of command-substitution nesting is resolved for `kill $(...)`; a
    substitution containing a further nested substitution is not recursed into.
  - The pipeline-scan classifies the pattern from the EARLIEST `pgrep`/`grep`/`egrep`/`fgrep`
    stage in a group that also has a kill-capable LAST stage; an exotic multi-grep pipeline
    with the real pattern in a later stage is not specially handled.
  - A shell alias for `pkill`/`kill`/`killall` is not resolved (same documented gap as every
    sibling hook in this catalog — aliases don't expand under a harness's `bash -c` anyway).

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a collateral pattern-kill slipping
through a broken guard is exactly the failure this hook exists to stop.
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
HOOK_ID = "pkill-guard"

# ── shared/ambiguous process names (the denylist) ───────────────────────────────────────────
# Generic tool/process names common on an AI-agent dev machine (this repo's own toolchain plus
# the sibling repos it works alongside). Matched as a whole word/phrase, case-insensitive.
# Multi-word entries (e.g. "review diff") are the literal incident shape: review-cli's own
# process command line legitimately starts with that phrase in EVERY session running it.
_SHARED_PROCESS_NAMES = frozenset({
    "node", "bun", "deno", "python", "python3", "electron", "code", "codex", "claude",
    "opencode", "review", "review diff", "playwright", "vsce", "npm", "npx", "tsx", "ts-node",
    "vitest", "jest", "pytest", "chrome", "chromium", "vite", "webpack", "esbuild", "tg",
    "tg-ctl", "cursor", "windsurf", "java", "ruby", "cargo", "rustc", "go", "docker",
    "gh", "rig",
})

# Sorted longest-first so a multi-word entry ("review diff") is tried before its shorter
# single-word substring ("review") would otherwise match first inside the same regex scan.
_NAME_PATTERNS = [
    (name, re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE))
    for name in sorted(_SHARED_PROCESS_NAMES, key=len, reverse=True)
]

# A session-scoping signal: a path separator, a long hex/alnum run (uuid/hash-shaped, e.g. a
# worktree hash or the harness's `hvsc-` isolation prefix + suffix), or a 4+ digit run (a port,
# a PID, a timestamp). Any one of these narrows the match set enough to trust the pattern.
_SCOPED_HEX_RUN = re.compile(r"[0-9a-fA-F]{6,}")
_SCOPED_DIGIT_RUN = re.compile(r"\d{4,}")


def _looks_session_scoped(pattern: str) -> bool:
    return (
        "/" in pattern
        or bool(_SCOPED_HEX_RUN.search(pattern))
        or bool(_SCOPED_DIGIT_RUN.search(pattern))
    )


def _dangerous_name_in(pattern: str) -> str | None:
    """The first denylisted name found as a whole word/phrase in `pattern`, or None."""
    for name, rx in _NAME_PATTERNS:
        if rx.search(pattern):
            return name
    return None


def _is_dangerous_pattern(pattern: str) -> str | None:
    """The denylisted name this pattern is dangerous for, or None if it's safe to let through
    (either it names nothing on the denylist, or it carries a session-scoping signal)."""
    if _looks_session_scoped(pattern):
        return None
    return _dangerous_name_in(pattern)


# ── argv recovery: shell segments, wrappers, inline env (same discipline as block-reset-hard) ──

_SHELL_SEPS = frozenset({"&&", "||", ";", "&"})  # "hard" separators: start a NEW pipeline group
_PIPE_SEPS = frozenset({"|", "|&"})  # separators that continue the SAME pipeline group
_ALL_SEPS = _SHELL_SEPS | _PIPE_SEPS

_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "if", "then", "do", "else", "elif"})
_MAX_LEADING_NOISE = 16

_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer",
    "command", "sudo", "doas", "exec",
})
_MAX_WRAPPER_NESTING = 16

# Wrapper flags that take a SEPARATE value (so the next token is the value, not the real
# command). A conservative subset of block-reset-hard's audited table — enough to not
# misparse the common `sudo -u user pkill ...` / `timeout -s SIG ...` shapes.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "--user", "-g", "--group"}),
    "doas": frozenset({"-u"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "env": frozenset({"-u", "--unset"}),
}
_OPERAND_DROP_WRAPPERS = frozenset({"timeout"})  # `timeout 5s <cmd>` — drop the duration operand


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _substitution_spans(text: str) -> list[tuple[int, int, str]]:
    """Every top-level `$( ... )` / `` ` ... ` `` span in `text`, as `(start, end, inner)`,
    quote-agnostic (this is a best-effort scan over raw text, before shlex tokenizing — see
    module docstring's "one level of nesting" limitation)."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$(", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                spans.append((i, j, text[i + 2 : j - 1]))
                i = j
                continue
            break  # unterminated — stop scanning, nothing more can be resolved
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                break
            spans.append((i, j + 1, text[i + 1 : j]))
            i = j + 1
            continue
        i += 1
    return spans


def _placeholder_command(command: str) -> tuple[str, dict[str, str]]:
    """Replace every top-level command substitution with an opaque placeholder token, so shlex
    tokenizes `kill $(pgrep -f X)` as `['kill', '__SUBST0__']` instead of splitting the
    substitution's own internal whitespace into separate argv tokens. Returns the rewritten
    command and a `{placeholder: inner_text}` side table."""
    spans = _substitution_spans(command)
    if not spans:
        return command, {}
    out: list[str] = []
    table: dict[str, str] = {}
    pos = 0
    for idx, (start, end, inner) in enumerate(spans):
        out.append(command[pos:start])
        placeholder = f"__PKILLGUARD_SUBST{idx}__"
        table[placeholder] = inner
        out.append(placeholder)
        pos = end
    out.append(command[pos:])
    return "".join(out), table


def _shlex_tokens(line: str) -> list[str] | None:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a (possibly multi-line) command into a flat token stream where a bare
    newline is a command separator (`;`) — same discipline as `block-reset-hard`. A plain
    single-pass shlex treats `\\n` as ordinary whitespace (a word separator, not a command
    separator), so `cd /repo` + newline + `pkill -f node` would otherwise collapse into ONE
    segment (`['cd', '/repo', 'pkill', '-f', 'node']`, argv[0]=`cd`) and hide the pkill entirely
    — this tokenizes LINE BY LINE and inserts an explicit `;` between lines, merging forward
    into the next line only when a line's own quotes don't balance (a value that legitimately
    spans a real newline). Returns None only if no split of the remaining lines ever balances
    (caller fails closed)."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n").replace("\\\n", "")
    lines = joined.split("\n")
    out: list[str] = []
    first = True
    i = 0
    while i < len(lines):
        chunk = lines[i]
        toks = _shlex_tokens(chunk)
        while toks is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
            toks = _shlex_tokens(chunk)
        if toks is None:
            return None
        if not first:
            out.append(";")
        first = False
        out.extend(toks)
        i += 1
    return out


def _split_groups(command: str) -> list[list[list[str]]] | None:
    """Tokenize `command` and split into pipeline GROUPS (separated by `;`/`&&`/`||`/`&`), each
    a list of STAGES (separated by `|`/`|&`). Returns None on unbalanced quotes."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    groups: list[list[list[str]]] = []
    stages: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _PIPE_SEPS:
            stages.append(current)
            current = []
        elif tok in _SHELL_SEPS:
            stages.append(current)
            groups.append(stages)
            stages = []
            current = []
        else:
            current.append(tok)
    stages.append(current)
    groups.append(stages)
    return [[s for s in g if s] for g in groups if any(g)]


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_inline_env(argv: list[str]) -> list[str]:
    while argv and _INLINE_ENV.match(argv[0]):
        argv = argv[1:]
    return argv


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i] in value_flags and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        i += 1
    return argv[i:]


class _WrapperOverflow(Exception):
    """A wrapper chain still unresolved past the nesting cap — fail closed (see block below)."""


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            raise _WrapperOverflow(f"wrapper chain exceeds {_MAX_WRAPPER_NESTING} levels")
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        argv = _strip_inline_env(argv)
    return argv


def _stage_argv(stage: list[str]) -> list[str]:
    argv = _strip_leading_shell_noise(stage)
    argv = _strip_inline_env(argv)
    argv = _strip_wrappers(argv)
    return argv


# ── per-command-shape pattern extraction ─────────────────────────────────────────────────────

_SIGNAL_FLAG = re.compile(r"^-(?:[A-Z0-9]+|s|-signal)$", re.IGNORECASE)


def _kill_is_pid_only(args: list[str]) -> bool:
    """True iff every non-flag argument to `kill` is a bare PID or job-spec (`%1`) — the always-
    safe form. A `-SIGNAL`/`-s SIGNAL` flag (and its value) is skipped."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            continue
        if a.startswith("-"):
            if a in ("-s", "--signal") and i + 1 < len(args):
                i += 2
                continue
            if _SIGNAL_FLAG.match(a):
                i += 1
                continue
            return False  # an unrecognized flag — don't claim to understand this invocation
        if a.startswith("%") or a.isdigit():
            i += 1
            continue
        return False
    return True


def _last_positional(args: list[str]) -> str | None:
    """The last non-flag token in `args` — pkill/killall's pattern/name argument. Flags with a
    glued value (`-SIGKILL`) are skipped whole; this is a best-effort scan, not a full getopt
    model (matches this hook's "shared name denylist, fail open on the unrecognized" posture)."""
    positional = [a for a in args if not a.startswith("-")]
    return positional[-1] if positional else None


def _pattern_for_stage(argv: list[str]) -> str | None:
    """If `argv` is a `pgrep`/`grep`/`egrep`/`fgrep` invocation, its pattern argument."""
    if not argv:
        return None
    name = _basename(argv[0])
    if name in ("pgrep", "grep", "egrep", "fgrep"):
        return _last_positional(argv[1:])
    return None


_XARGS_VALUE_FLAGS = frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "--delimiter"})


def _xargs_target_is_kill(argv: list[str]) -> bool:
    """True iff `argv` is an `xargs` invocation whose wrapped command is `kill`."""
    if not argv or _basename(argv[0]) != "xargs":
        return False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and a != "--":
            if a in _XARGS_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        return _basename(a) == "kill"
    return False


def _stage_kills(argv: list[str]) -> bool:
    """True iff `argv` is a stage that actually issues the kill (bare `kill`, or `xargs kill`)."""
    if not argv:
        return False
    return _basename(argv[0]) == "kill" or _xargs_target_is_kill(argv)


def _classify_group(group: list[list[str]], subst_table: dict[str, str]) -> str | None:
    """Classify one pipeline group (list of raw stages). Returns the DANGEROUS pattern's source
    name if this group is a pattern-based kill of a shared name that isn't session-scoped, else
    None (safe or not a kill at all)."""
    stage_argvs = [_stage_argv(s) for s in group]

    # Direct pkill/killall — pattern is the stage's own trailing positional(s).
    for argv in stage_argvs:
        if not argv:
            continue
        name = _basename(argv[0])
        if name in ("pkill", "killall"):
            pattern = _last_positional(argv[1:])
            if pattern is None:
                continue
            dangerous = _is_dangerous_pattern(pattern)
            if dangerous:
                return dangerous

    # `kill` fed a pgrep/backtick substitution.
    for argv in stage_argvs:
        if not argv or _basename(argv[0]) != "kill":
            continue
        rest = argv[1:]
        if _kill_is_pid_only(rest):
            continue
        for tok in rest:
            if not tok.startswith("__PKILLGUARD_SUBST"):
                continue
            inner_groups = _split_groups(subst_table.get(tok, ""))
            if not inner_groups:
                continue
            for inner_group in inner_groups:
                inner_argvs = [_stage_argv(s) for s in inner_group]
                pattern = _pattern_group_pattern(inner_argvs)
                if pattern is not None:
                    dangerous = _is_dangerous_pattern(pattern)
                    if dangerous:
                        return dangerous

    # Pipeline: an earlier pgrep/grep stage feeding a later kill/xargs-kill stage.
    if len(stage_argvs) >= 2 and any(_stage_kills(a) for a in stage_argvs):
        pattern = _pattern_group_pattern(stage_argvs)
        if pattern is not None:
            dangerous = _is_dangerous_pattern(pattern)
            if dangerous:
                return dangerous

    return None


def _pattern_group_pattern(stage_argvs: list[list[str]]) -> str | None:
    """The pattern named by the FIRST pgrep/grep-family stage in `stage_argvs` (a pipeline's
    stages, already argv-resolved)."""
    for argv in stage_argvs:
        pattern = _pattern_for_stage(argv)
        if pattern is not None:
            return pattern
    return None


def _classify(command: str) -> tuple[str, str | None]:
    """Classify `command`. Returns (verdict, dangerous_name):
      "safe"        — no pattern-kill of a shared, unscoped name found.
      "dangerous"   — a pattern-kill of `dangerous_name` was found.
      "unparseable" — shlex could not tokenize AND the raw text plausibly contains a kill of
                      a denylisted name — fail-closed.
    """
    rewritten, subst_table = _placeholder_command(command)
    groups = _split_groups(rewritten)
    if groups is None:
        hint = _dangerous_name_in(command)
        if hint and re.search(r"\b(pkill|killall|kill|pgrep)\b", command):
            return "unparseable", hint
        return "safe", None
    try:
        for group in groups:
            dangerous = _classify_group(group, subst_table)
            if dangerous:
                return "dangerous", dangerous
    except _WrapperOverflow:
        return "unparseable", None
    return "safe", None


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _block_message(dangerous_name: str) -> str:
    return (
        f"Refusing a pattern-based kill of \"{dangerous_name}\": `pkill -f`/`killall`/`pgrep` "
        "match against EVERY process on this machine whose command line contains that text, "
        "not just yours — this has already killed a DIFFERENT session's in-flight work twice "
        "(a code review, an e2e matrix run). Use a PID-targeted `kill <pid>`, `dev stop` for "
        "this repo's dev/e2e processes, or scope the pattern to something session-unique (a "
        "worktree path, the harness's isolation-prefix directory).\n"
        "There is no automatic bypass. For a genuine exception, ASK the human, or request a "
        f"one-time Telegram approval: RIG_HATCH_REQUEST_PKILL_GUARD=\"<reason>\" <command>."
    )


def _gate_dangerous(dangerous_name: str, cwd: str, command: str) -> int:
    context = {"hook": HOOK_ID, "kind": "pattern-kill", "target": dangerous_name, "command": command}
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, context, cwd=cwd, command=command)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"pattern-kill of {dangerous_name!r} approved via hatch escalation ({hatch.reason})")
            emit("allow", f"approved via hatch escalation ({hatch.reason})")
            return 0
        warn(f"pattern-kill of {dangerous_name!r} hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{_block_message(dangerous_name)}")
        return BLOCK_EXIT_CODE
    emit("block", _block_message(dangerous_name))
    return BLOCK_EXIT_CODE


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", f"{HOOK_ID}: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    verdict, dangerous_name = _classify(command)

    if verdict == "safe":
        emit("allow")
        return 0

    if verdict == "unparseable":
        warn("could not verify this command is not a pattern-kill of a shared name — blocking (fail-closed)")
        emit(
            "block",
            f"{HOOK_ID}: could not verify this command is not a pattern-based kill of a "
            "shared process name (fail-closed). Split it into a simpler Bash call.",
        )
        return BLOCK_EXIT_CODE

    assert dangerous_name is not None  # verdict == "dangerous" always carries a name
    return _gate_dangerous(dangerous_name, cwd, command)


if __name__ == "__main__":
    sys.exit(main())
