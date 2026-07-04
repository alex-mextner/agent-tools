#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block `git reset --hard` and `git clean -f...`.

Denies a shell command that irreversibly wipes uncommitted/untracked work with zero undo:

  - `git reset --hard` (with or without a ref) — discards uncommitted TRACKED changes.
  - `git clean` with a real `-f`/`--force` flag (alone or clustered with `-d`/`-x`, in any
    short-flag order) — discards untracked files.

Both are the same failure class: a destructive rewrite of the working tree that git cannot
undo afterward (unlike `git reset` without `--hard`, which never touches the working tree,
or `git clean -n`, which only previews). This hook exists because an incident showed nothing
technically stops an agent from running either mid-session: a subagent working an unrelated
PR ran `git reset --hard` in a checkout shared with a different session and wiped that
session's uncommitted work. The incident was ACCIDENTAL, not a deliberate bypass — this
hook's real value is turning an accidental destructive reset/clean into a DELIBERATE,
LOGGED one. It is not a hard wall: the escape hatch below is intentionally self-service,
same as every sibling hook in this family.

Detection is ARGV-BASED, not a raw substring match (same discipline as block-raw-pr-merge /
block-no-verify / require-ticket-before-commit). The command is tokenized LINE BY LINE (a
newline is a command separator, same as `;` — a two-line Bash call is a common, entirely
accidental shape: `cd /repo` then `git reset --hard` on the next line is the literal incident
this hook guards against) with a word-boundary-aware `#` comment scan (a `#` mid-word, e.g.
`foo#bar`, is literal text to a real shell, NOT a comment — shlex's default commenter gets
this wrong and would truncate parsing there, silently hiding a later chained command; ported
fix from block-no-verify/require-review-before-commit), then split into shell segments (`;` /
`&&` / `||` / `|` separated). Each segment's real argv is recovered by stripping leading
shell-grouping/control-flow tokens (`(`, `{`, `if`, `while`, `case`, ...), inline `VAR=value`
env assignments, a set of common wrapper executables (`timeout`, `sudo`, `env`, `nice`,
`time`, `exec`, `command`, `setsid`, `nohup`, ... — ported from block-no-verify /
require-ticket-before-commit's wrapper table, since a destructive command hidden behind
`sudo -u user git reset --hard` or `time git clean -fd` is exactly the bypass shape those
sibling hooks already fought), and git's own global options (`-C`, `-c`, `--git-dir`, ...) so
the git subcommand is exposed even behind `git -C <dir> reset --hard`. This prevents a false
positive where a commit message, code comment, doc, or `grep` merely MENTIONS the phrase
"reset --hard" or "clean -fd" as text.

Allowed (let through — the safe alternatives this hook steers agents toward):
  - `git checkout -- <file>` / `git restore <file>` (discard specific tracked files, scoped)
  - `git reset` with no `--hard` (bare, `--mixed`, `--soft`) — never touches the working tree
  - `git clean -n` / `git clean --dry-run` (preview only)
  - `git clean` with no force flag at all (git itself refuses to delete without `-f`)
  - text that merely mentions "reset --hard" or "clean -fd" (commit message, comment, grep)

Escape hatch (controllable, not a hard wall — mirrors block-raw-pr-merge):
  - env  ALLOW_GIT_RESET_HARD=1                 — disable the guard for this session
  - env  ALLOW_GIT_RESET_HARD_REASON=...        — REQUIRED with the override; the reason is
    logged. Gates BOTH the reset --hard block and the clean -f block (one hatch, one name).
  - inline sentinel  `# no-reset-guard: <reason>`  anywhere in the command also overrides,
    so a one-off deliberate reset/clean is self-documenting in the command itself.
  An override with no reason still blocks: a silent bypass of the bypass-guard is the very
  thing this hook exists to prevent.

Known limitations — the escape hatch is ALREADY a deliberate, self-service bypass (by
design: see above), so hardening the parser against an adversarial agent that WANTS through
is incoherent — it would just use the hatch. The bar for what's fixed above vs. documented
below is therefore: would a confused, non-evasive agent produce this exact command BY
ACCIDENT? Newline-separated commands and mid-word `#` (fixed above) clear that bar; these
don't:
  - `git reset --har` (an unambiguous long-option prefix git itself accepts) is not detected;
    only the literal `--hard` spelling is matched — no one accidentally abbreviates a
    destructive flag they're not trying to type in full.
  - `git clean -e -f` (a bare `-e` immediately followed by a SEPARATE `-f`-shaped token) is
    conservatively treated as a real `-f` (blocked) by this hook, even though real git parses
    that trailing token as `-e`'s exclude-pattern VALUE (confirmed empirically: `git clean -e
    -f` prints "clean.requireForce is true and -f not given: refusing to clean" — i.e. real
    git does NOT treat it as force). This is a FALSE BLOCK, not a bypass — the safe-by-default
    direction for a destructive-action guard, and not worth getopt-style value tracking for
    such an exotic input shape.
  - `git -c clean.requireForce=false clean -d` (or the same set as an AMBIENT gitconfig, not
    inline) makes a bare `clean -d` — no `-f` at all — actually delete, contradicting this
    hook's "no force flag = git refuses" assumption. Not detected: nobody sets this inline by
    accident, but if it's ALREADY in someone's ambient config, a bare `clean -d` silently
    slips past this hook for real. Worth knowing, not worth building a git-config-value
    detector for.
  - The inline `# no-reset-guard: <reason>` sentinel is matched against the WHOLE raw command
    string, including inside quotes — `git commit -m "notes: no-reset-guard: x" && git reset
    --hard` would be read as a valid override even though the text is commit-message data, not
    a real shell comment on the dangerous segment. Requires a crafted message to trigger; the
    hatch is self-service anyway, so scoping the sentinel to a genuine comment token isn't
    worth the added parsing machinery here.
  - A shell alias for `git` (`alias g=git`) is not resolved — universal to this whole hook
    family (aliases only expand in an INTERACTIVE shell by default; a harness running via
    `bash -c "<command>"` doesn't expand them anyway, so this is rarely reachable in practice).
  - `env -S/--split-string '<command>'` (a re-tokenized inline command string), a nested
    shell-string interpreter (`bash -c 'git reset --hard'`), a command substitution
    (`$(git reset --hard)`), or a wrapper outside a brace group are not re-parsed — same
    documented gap as block-no-verify; all require deliberately crafted input, not an
    accidental shape.
  - An unlisted/exotic wrapper (`unshare`, `nsenter`, `firejail`, ...) is not peeled.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a destructive reset/clean slipping
through a broken guard is exactly the failure this hook exists to stop.
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
# E.g. `( git reset --hard )`, `{ git reset --hard; }`, `while git clean -fd; do ...`.
# Mirrors block-no-verify / require-ticket-before-commit's `_LEADING_SHELL_NOISE`.
_LEADING_SHELL_NOISE = frozenset(
    {"(", "{", "!", "if", "then", "do", "else", "elif", "while", "until", "for", "case"}
)
_MAX_LEADING_NOISE = 16  # cap so an all-noise segment can't loop

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Wrapper executables that prefix the REAL command and pass the rest through unchanged.
# Ported from block-no-verify / require-ticket-before-commit's wrapper table — a destructive
# `git reset --hard`/`git clean -f...` hidden behind `sudo -u user`, `time`, `exec`, `command`,
# `setsid`, `nohup`, etc. is exactly the bypass shape those sibling hooks already fought.
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer",
    "command", "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16

# Wrappers that take a LEADING POSITIONAL operand before the real command (timeout's
# duration, chrt's priority, taskset's mask, flock's lockfile).
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
# Operand-drop wrappers whose operand is a MANDATORY arbitrary string that may legitimately be
# `git` (flock's lockfile) — dropped even when literally `git`.
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})

# Per-wrapper flags taking a SEPARATE value, so the NEXT token is that value, not the wrapped
# command (`sudo -u USER git ...`, `timeout -s SIG ...`). Ported verbatim from
# block-no-verify/require-ticket-before-commit's audited table (the comments there record why
# each entry exists — a missing separate-operand flag silently shifts the parse and re-opens
# the bypass).
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-r", "--role",
        "-t", "--type", "-C", "--close-from", "-R", "--chroot", "-D", "--chdir",
        "-T", "--command-timeout", "-U", "--other-user", "-c", "--login-class",
        "-a", "--auth-type",
    }),
    "doas": frozenset({"-u", "-a", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({
        "-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid",
    }),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "exec": frozenset({"-a"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "taskset": frozenset({"-c", "--cpu-list"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
    "setpriv": frozenset({
        "--ruid", "--euid", "--reuid", "--rgid", "--egid", "--regid", "--groups", "--ptracer",
        "--securebits", "--pdeathsig", "--ambient-caps", "--inh-caps", "--bounding-set",
        "--selinux-label", "--apparmor-profile", "--landlock-access", "--landlock-rule",
    }),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}),
    "runuser": frozenset({
        "-c", "--command", "--session-command", "-g", "--group", "-G", "--supp-group",
        "-s", "--shell", "-u", "--user", "-w", "--whitelist-environment",
    }),
}
# INVARIANTS (a loud import-time failure, not a silent bypass): every operand-drop /
# value-flag / mandatory-operand wrapper must first be a recognized wrapper, else its config
# is dead and the wrapped command slips through (argv[0] != git → not gated).
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError(f"operand-drop wrappers missing from _WRAPPERS: {_OPERAND_DROP_WRAPPERS - _WRAPPERS}")
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError(f"mandatory-operand wrappers not in operand-drop: {_MANDATORY_OPERAND_WRAPPERS - _OPERAND_DROP_WRAPPERS}")
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError(f"value-flag wrappers missing from _WRAPPERS: {set(_WRAPPER_VALUE_FLAGS) - _WRAPPERS}")


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    """The single-letter forms (`-u` -> `u`) of a wrapper's value-flags, for a combined short
    cluster ending in a value-letter (`sudo -up alice` = `-u -p alice`... `-uP` = `-u P`)."""
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}

# git global options that take a separate value and may appear before the subcommand,
# e.g. `git -C /path reset --hard`. Mirrors block-no-verify's `_GIT_GLOBAL_VALUE_FLAGS`.
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

# Reset-hard hint: used to gate fail-closed on unbalanced-quote parse errors so that a
# benign command with an unbalanced quote is NOT blocked just because shlex can't parse it.
# Only if the raw string plausibly contains a reset --hard or clean -f invocation do we
# treat a parse error as fail-closed.
_RESET_HARD_HINT = re.compile(r"\bgit\b.*\breset\b.*\bhard\b", re.DOTALL)
_CLEAN_FORCE_HINT = re.compile(r"\bgit\b.*\bclean\b.*(-f|--force)", re.DOTALL)

# Inline, self-documenting per-command override (checked on the raw string so a
# comment stripped by shlex is still found).
INLINE_SENTINEL = re.compile(r"#\s*no-reset-guard:\s*(\S.*)")


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-reset-hard: {msg}\n")


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/git` -> `git`, `./git` -> `git`."""
    return tok.rsplit("/", 1)[-1]


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line. ``punctuation_chars=True`` splits real shell separators
    (`;`, `&&`, `||`, `|`, ...) into standalone tokens while honoring quotes.

    Comments are handled MANUALLY (shlex's ``commenters`` is disabled) — shlex's built-in
    commenter cuts at ANY unquoted `#`, even mid-word, which would truncate parsing at a
    benign `foo#bar` and silently hide a LATER `&& git reset --hard` on the same line (an
    accidental, non-adversarial miss — the exact class this hook exists to catch, ported
    from block-no-verify/require-review-before-commit's audited fix for the same bug). A
    token that STARTS with `#` is a real word-initial comment; drop it and the rest of the
    line. Returns None on unbalanced quotes (caller treats this as fail-closed).
    """
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    out: list[str] = []
    for tok in tokens:
        if tok.startswith("#"):
            break
        out.append(tok)
    return out


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a
    NEWLINE is a command separator (a `;` token between lines). A two-line Bash call
    (`cd /repo` then `git reset --hard` on the next line) is a common, entirely accidental
    shape — a single flat shlex pass over the whole string misses it (the newline is just
    whitespace to shlex, so `cd`/`echo` on line one becomes argv[0] and the real command on
    line two is invisible). Returns None only if a chunk can never balance (fail-closed).
    """
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")  # honor backslash-newline line continuations
    lines = joined.split("\n")
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
            return None
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _split_segments(command: str) -> list[list[str]]:
    """Tokenize `command` (possibly multi-line) and split into per-segment token lists. Each
    segment is one independent command. Raises ValueError on unbalanced quotes/unterminated
    lines (caller treats this as fail-closed).
    """
    tokens = _tokenize(command)
    if tokens is None:
        raise ValueError("unbalanced quotes or unterminated line")
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


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading subshell/brace openers and control keywords so a command introduced by
    them is recovered: `(git reset --hard)` -> `git reset --hard`. Capped so an all-noise
    segment can't loop.
    """
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_inline_env(argv: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off `argv`, returning the rest starting at the
    executable (`HUSKY=0 sudo git reset --hard` -> `["sudo", "git", "reset", "--hard"]`)."""
    while argv and _INLINE_ENV.match(argv[0]):
        argv = argv[1:]
    return argv


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    """True when a short cluster's LAST char is a value-letter for this wrapper AND no
    earlier value-letter already consumed the cluster's tail (`-iP` -> True; `-Px` -> False)."""
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    """Drop one wrapper's own option flags + (for an operand-drop wrapper) its leading
    positional operand, returning argv positioned at the wrapped command. A flag in this
    wrapper's value set (`sudo -u alice`) consumes the following token UNCONDITIONALLY — even
    when it is literally `git` (`sudo -u git git reset --hard`: the user is named "git", the
    SECOND git is the executable).
    """
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    short_letters = _WRAPPER_SHORT_VALUE_LETTERS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        consumes_next = (
            argv[i] in value_flags
            or (not argv[i].startswith("--") and _cluster_takes_next_value(argv[i], short_letters))
        )
        if consumes_next and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or _basename(argv[i]) != "git":
            i += 1
    return argv[i:]


class _WrapperOverflow(Exception):
    """Raised when a wrapper chain is still unresolved past `_MAX_WRAPPER_NESTING` — the real
    command was never reached. This hook is `on_error: closed`, so an unresolvable wrapper
    chain must be treated as UNVERIFIABLE and fail closed (block), NOT silently treated as
    safe just because peeling gave up. A sibling hook (`require-ticket-before-commit`) is
    `on_error: open` and deliberately returns argv unchanged past the cap — that choice is
    wrong FOR THIS HOOK, where "couldn't check" must mean "don't allow it".
    """


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading wrapper executables (`timeout 60`, `sudo -u alice`, `env FOO=bar`, ...) so
    the REAL command beneath is what we inspect. Stops as soon as the head is no longer a
    known wrapper, so a real `git` is never skipped. `env -S '<command>'` split-string is NOT
    recursed here (a documented best-effort limitation, matching the sibling hooks). Raises
    `_WrapperOverflow` past the nesting cap (fail-closed — see the class docstring).
    """
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            raise _WrapperOverflow(f"wrapper chain exceeds {_MAX_WRAPPER_NESTING} levels")
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        argv = _strip_inline_env(argv)  # `sudo FOO=bar git ...` / `env HUSKY=0 git ...`
    return argv


def _segment_argv(segment: list[str]) -> list[str]:
    """Return the real argv by stripping leading shell noise, inline env assignments, and
    known command wrappers, so `( TIMEOUT=1 timeout 5 sudo git reset --hard )` resolves to
    `["git", "reset", "--hard"]`.
    """
    argv = _strip_leading_shell_noise(segment)
    argv = _strip_inline_env(argv)
    argv = _strip_wrappers(argv)
    return argv


def _strip_git_globals(argv: list[str]) -> list[str]:
    """Given argv starting at the git executable, return argv starting at the subcommand,
    skipping git's own global options (`-C <dir>`, `-c key=val`, `--git-dir=...`,
    `--no-pager`, glued `-C<dir>`/`-c<k>=<v>`, ...).
    """
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if not tok.startswith("-"):
            break
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2  # flag + its separate value
            continue
        if tok.startswith(("-C", "-c")) and len(tok) > 2:
            i += 1  # glued form: -C/path, -ckey=val
            continue
        i += 1  # any other global flag (--no-pager, -p, --literal-pathspecs, ...)
    return argv[i:]


def _git_subcommand(segment: list[str]) -> list[str] | None:
    """Return the git subcommand argv (e.g. `["reset", "--hard"]`) for this segment, or
    None if the segment is not a git invocation.
    """
    argv = _segment_argv(segment)
    if not argv or _basename(argv[0]) != "git":
        return None
    return _strip_git_globals(argv)


def _is_reset_hard(segment: list[str]) -> bool:
    """Return True iff this segment is `git reset --hard [ref]` (globals/wrappers peeled)."""
    sub = _git_subcommand(segment)
    return bool(sub) and sub[0] == "reset" and "--hard" in sub[1:]


def _has_force_flag(tokens: list[str]) -> bool:
    """Return True iff `tokens` (the args after `git clean`) contain a real `-f`/`--force`.

    Handles clustered short flags (`-fd`, `-df`, `-fdx`, `-xdf`) in any order, and stops
    scanning a cluster at `e` since `-e<pattern>` consumes the rest of that token as its
    exclude-pattern VALUE, not further flag letters (`-ef*.o"` is `-e` with value `f*.o"`,
    not `-e` plus `-f`; `-fe*.o"` IS force — `f` comes before `e`).
    """
    for tok in tokens:
        if tok == "--":
            break  # remaining tokens are pathspecs, not flags
        if tok == "--force":
            return True
        if tok.startswith("--"):
            continue  # another long flag (--dry-run, --exclude=..., --interactive, ...)
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "f":
                    return True
                if ch == "e":
                    break  # -e consumes the rest of this token as its pattern value
    return False


def _is_clean_force(segment: list[str]) -> bool:
    """Return True iff this segment is `git clean` with a real force flag (globals peeled)."""
    sub = _git_subcommand(segment)
    return bool(sub) and sub[0] == "clean" and _has_force_flag(sub[1:])


def _classify(command: str) -> tuple[str, str | None]:
    """Classify `command` for the two dangerous forms this hook guards.

    Returns (verdict, kind):
      verdict "safe"        — command parsed cleanly, no reset --hard / clean -f... found.
      verdict "dangerous"   — command parsed cleanly and a segment IS one of the two forms.
      verdict "unparseable" — shlex could not tokenize (unbalanced quotes) AND the raw text
                              plausibly contains one of the dangerous forms — fail-closed.
    `kind` is "reset --hard" or "clean -f..." whenever verdict != "safe", else None.
    """
    try:
        segments = _split_segments(command)
    except ValueError:
        if _RESET_HARD_HINT.search(command):
            return "unparseable", "reset --hard"
        if _CLEAN_FORCE_HINT.search(command):
            return "unparseable", "clean -f..."
        return "safe", None
    for seg in segments:
        try:
            if _is_reset_hard(seg):
                return "dangerous", "reset --hard"
            if _is_clean_force(seg):
                return "dangerous", "clean -f..."
        except _WrapperOverflow:
            # Can't resolve this segment's real command through the wrapper chain — the
            # command MIGHT be a destructive reset/clean hidden behind it. Fail closed.
            return "unparseable", "wrapper chain too deep to verify"
    return "safe", None


def _override_reason(command: str) -> str | None:
    """Return the override reason if a valid escape hatch is present, else None.

    An override is honored ONLY with a reason: env ALLOW_GIT_RESET_HARD=1 plus
    ALLOW_GIT_RESET_HARD_REASON, OR an inline ``# no-reset-guard: <reason>`` sentinel. Gates
    BOTH `reset --hard` and `clean -f...` — one escape hatch, one name. A reasonless override
    is ignored (the command stays blocked).
    """
    if os.environ.get("ALLOW_GIT_RESET_HARD") == "1":
        reason = (os.environ.get("ALLOW_GIT_RESET_HARD_REASON") or "").strip()
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
        emit("block", "block-reset-hard: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    verdict, kind = _classify(command)

    if verdict == "safe":
        emit("allow")
        return 0

    if verdict == "unparseable":
        warn(f"could not verify command ({kind}) — blocking (fail-closed)")
        emit(
            "block",
            f"block-reset-hard: could not verify this command is not a destructive "
            f"`git reset --hard`/`git clean -f...` ({kind}) — blocking (fail-closed).",
        )
        return BLOCK_EXIT_CODE

    # verdict == "dangerous": a cleanly-parsed segment is a real reset --hard / clean -f...
    reason = _override_reason(command)
    if reason:
        warn(f"{kind} allowed via escape hatch ({reason})")
        emit("allow", f"{kind} allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        f"Refusing a `git {kind}`: it irreversibly wipes uncommitted/untracked work with no "
        "undo. Use a scoped, reversible alternative instead — `git checkout -- <file>` / "
        "`git restore <file>` for tracked files, `git clean -n` to preview untracked files "
        "first. Override only with an explicit reason: set ALLOW_GIT_RESET_HARD=1 and "
        "ALLOW_GIT_RESET_HARD_REASON='why', or append `# no-reset-guard: why` to the command.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
