#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block ripgrep's `--pre` (arbitrary command execution),
ARGV-PARSED, not glob/substring-matched.

`rg --pre COMMAND` runs COMMAND as an arbitrary preprocessor on EVERY matched file — turning the
read-only search tool every agent is pre-allowed to run (`Bash(rg:*)`) into an unrestricted
local code-execution vector. This is the deep layer the coarse per-harness glob/prefix deny
rules (rig-cli's `riglib/permissions.py` — `CLAUDE_CODE_DENY_RULES`, `OPENCODE_DENY_RULES`,
`OMP_GUARD_DENY_RULES`) cannot fully express, named there as a tracked follow-up:

    "UNLIKE `--no-verify`, though, `rg-pre` has no claude-code-specific argv-precise hook
    backstopping this glob belt yet ... An analogous claude-code `block-rg-pre` agent-hook is a
    real, tracked follow-up (agent-tools side), not attempted in this fix."

Two concrete gaps in the glob belt this hook closes:
  1. It cannot honor `--` end-of-options: `rg -- --pre .` (a literal, harmless search for the
     text "--pre") is denied by the glob rule even though `--pre` there is a POSITIONAL argument,
     not a flag.
  2. It cannot tell `--pre` used AS a flag from `--pre` appearing as the VALUE of a different
     flag: `rg -e --pre .` (searching for the literal pattern "--pre" via `-e`/`--regexp`) is
     denied by the glob rule even though no `--pre` flag is actually present.

NOTE on the interaction with the glob belt (found in review, tracked as a rig-cli follow-up, not
fixed here): on a machine where the coarse `Bash(rg * --pre *)`-style deny pattern is ALSO
installed (claude-code's own permission check runs independently of, and before, this PreToolUse
hook), gap 1/2's ALLOW verdict may not be visibly reached yet for that specific harness — the
glob's own substring match still denies `rg -- --pre .` on its own, regardless of this hook. This
hook still delivers its primary, load-bearing value even so: it is strictly MORE precise than any
glob (harnesses with a looser or absent glob belt get full protection today), and its BLOCK path
is unconditionally additive — it never allows something the glob would have blocked. Retiring or
narrowing the glob rules once this hook is deployed everywhere is a rig-cli-side coordination
task, out of scope for this repo.

Detection is ARGV-BASED (shlex), same discipline as `block-no-verify` / `pkill-guard` /
`block-raw-pr-merge`: the command is tokenized (a newline is a command separator, same as `;`),
split into segments on every shell separator (`;`/`&&`/`||`/`|`/`|&`/`&`/newline — this hook does
not need pkill-guard's pipe-vs-hard-separator distinction, since `rg --pre` is dangerous
regardless of its position in a pipeline), and each segment's real argv is recovered after
stripping leading shell-grouping tokens, shell redirections (value-flag-aware — see
`_is_rg_value_flag_before_redirect`), inline `VAR=value` assignments, and a wrapper table
(`sudo`, `timeout`, `env` — including `env -S`/`--split-string`, re-inspected as a nested command
— `nice`, `taskset`, `chrt`, `setpriv`, `runuser`, `flock`, `xargs`, ...), ported from
`block-no-verify`'s audited wrapper-peeling machinery (same discipline note there: each hook is a
self-contained standalone script with no shared import path, so this parser is duplicated by
design — keep it in step with the siblings when changing it).

A segment whose recovered argv[0] is `rg` (bare, or a path to it — `/opt/homebrew/bin/rg`; also
the WRAPPED command of an `xargs` invocation, e.g. `pgrep ... | xargs rg --pre ...`) is then
scanned LEFT TO RIGHT for a real `--pre`/`--pre=COMMAND` flag, stopping at a literal `--`
(everything after it is a positional pattern/path, never a flag) and correctly skipping the
VALUE of every OTHER value-taking rg flag (`-e`/`--regexp`, `-f`/`--file`, `-t`/`--type`, ...) so
that value is never misread as a `--pre` flag of its own. `--pre-glob=GLOB` is a SEPARATE,
non-dangerous flag (its glob has no effect without `--pre`, which is caught on its own) — never
conflated with `--pre` (exact-token match only, mirroring the glob belt's own comment on why
`--pre-glob` is deliberately ungated everywhere).

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a MALFORMED EVENT (bad JSON) always denies. A parse failure on the command
text itself denies whenever the raw text still plausibly names an `rg ... --pre` invocation or
looks like an obfuscated `env -S`; a parse failure on text that could not plausibly be either is
ALLOWED (nothing here for `--pre` to hide in) — see `_plausible_rg_pre`/`_looks_obfuscated` in
`main()`. This is a deliberate, narrower guarantee than "any crash denies everything" — it keeps
this hook from becoming a blanket parser for every `rg`-unrelated shell command that merely fails
to tokenize.

Known limitations (documented, not silently missed — same bar as sibling hooks):
  - No heredoc awareness (unlike `block-no-verify`): a heredoc BODY that happens to contain a
    line looking like `rg --pre ...` is treated as a real command — an OVER-block, never a
    bypass, same accepted trade-off as `pkill-guard`.
  - A command substitution used AS AN ARGUMENT to a directly-typed `rg` invocation (e.g. `rg
    "$(printf -- --pre)" ./x .`) IS caught: any unresolved `$(...)`/backtick token inside an rg
    segment's own argv fails the whole invocation CLOSED (`_has_unresolved_substitution`), since
    this hook cannot statically know what the shell will expand it to. What remains unresolved is
    the BROADER case — the entire `rg ... --pre ...` invocation itself hidden inside a
    substitution assigned elsewhere (`x=$(rg --pre evil .)`), where `argv[0]` is never literally
    `rg` to this hook at all. Same documented precision trade as every sibling hook's `bash -c
    '...'` limitation.
  - A shell alias for `rg` is not resolved (aliases don't expand under a harness's `bash -c`
    anyway — same gap as every sibling hook).
  - `find ... -exec rg --pre ... {} ';'` and `eval rg --pre ... .` are not resolved (unlike
    `xargs`, neither `find` nor `eval` is a recognized wrapper) — a `--pre` invocation reached
    either way is not inspected. Documented, not fixed here (the coarse glob belt still catches
    the raw text on harnesses that have one). `eval` is deliberately kept OUT of `_WRAPPERS`, not
    merely forgotten: that table is SYNC'd verbatim from `block-no-verify`, so adding `eval` only
    here would silently break the sync invariant the two hooks rely on. Tracked as agent-tools#421
    (add `eval` to both tables together, evaluate `find -exec` resolution, and add an automated
    check that the two SYNC'd tables actually stay identical — nothing currently enforces that).
  - ANSI-C quoting (`$'...'`) and other shell EXPANSIONS (`"$VAR"`, `${VAR}`, brace/glob
    expansion) are not interpreted — this hook reasons about the LITERAL argv shlex recovers, the
    same as every sibling hook. A payload assembled through an ANSI-C-quoted string or a shell
    variable set earlier in the SAME command is not resolved to its expanded form (e.g. `X='rg
    --pre CMD'; env -S "$X" ...` — the assignment and its later `"$X"` reference are two separate,
    unconnected tokens to this hook). Same class of gap `pkill-guard`'s docstring documents for
    `TARGET=node pkill -f "$TARGET"`: deliberately crafted evasion, not an accidental shape, and
    the raw text still trips the fail-closed `_plausible_rg_pre` check if tokenizing fails.
  - `RIPGREP_CONFIG_PATH` (or ripgrep's own default config-file discovery) can inject a `--pre`
    flag with NOTHING in the command's own argv — rg reads that file's flags itself, invisibly to
    any argv-level guard. This hook does not open and scan the referenced file (would grow it
    into a ripgrep config-file parser). A value set INLINE on the inspected command
    (`RIPGREP_CONFIG_PATH=/tmp/x.rc rg pattern .`, via the `env` wrapper, or via an earlier
    `export`/`declare` in the SAME command string — `export RIPGREP_CONFIG_PATH=x; rg needle .`
    — tracked forward through `exported` in `_find_dangerous_rg_pre`) fails CLOSED rather than
    trusting the unread file. What remains unfixed is a value that reaches rg WITHOUT appearing
    anywhere in the command string this hook actually inspects — set by a genuinely separate,
    earlier Bash tool call, or already present in whatever environment the agent's shell started
    with. CORRECTION (found in review): earlier revisions of this note claimed such a value is
    "invisible to this hook" outright; that overstated what's actually verified. What's true and
    checked (`lib/agent_hooks_v1/runner.py`'s `subprocess.run` call, no `env=` override) is that
    THIS HOOK'S OWN subprocess inherits *some* OS environment — namely the hook-bridge process's
    own, not necessarily the agent's interactive shell's. Whether an `export` the agent typed in
    a PRIOR, separate Bash call ends up in the hook-bridge process's environment depends on
    plumbing this hook cannot verify from where it runs (a separate process spawned by the
    harness, not the persistent shell the agent's commands run in) — not confirmed to leak
    through, and not confirmed to be blocked. Given that uncertainty, this hook does NOT read
    `os.environ` at all for this check (only the command string's own visible env), so it is
    conservative either way: it cannot be tricked by a real ambient value it merely failed to
    read, and it does not claim protection it hasn't verified. Gate `RIPGREP_CONFIG_PATH` /
    force `--no-config` at the provisioning layer (rig-cli) as a follow-up if this becomes a
    real vector.
  - No effective-state tracking across multiple `--pre`/`--pre=`/`--no-pre` occurrences in one
    invocation: `rg --pre CMD --no-pre .` (which ripgrep's own docs say disables the
    preprocessor) is still blocked, since the FIRST `--pre CMD` is enough to trigger a verdict.
    Over-block only (never a bypass) — deliberately not implemented; nobody legitimately writes
    this pattern, and modeling ripgrep's own last-wins flag precedence for a purely cosmetic
    edge case is not worth the added parsing surface.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "block-rg-pre"

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


# ── tokenization (SYNC'd from block-no-verify's audited parser, trimmed of git-specific bits) ──

_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset({
    "(", "{", "!", "then", "do", "else", "elif", "while", "until", "for", "case",
})
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, quote-aware — a `#` starts a comment only at a
    word boundary and only outside quotes, so a quoted argument that merely contains `#` is
    never truncated."""
    in_single = in_double = False
    prev_ws = True
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
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a (possibly multi-line) command into a flat token stream where a bare
    newline is a command separator (`;`). Returns None only if a chunk can never be balanced."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n").replace("\\\n", "")
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
            out.append(";")
        first = False
        out.extend(toks)
        i += 1
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
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


_MAX_LEADING_SHELL_NOISE = 16


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Strip up to `_MAX_LEADING_SHELL_NOISE` leading grouping tokens (`(`, `{`, `!`, ...). Unlike
    a plain truncating loop, a 17th-or-later noise token RAISES rather than silently returning a
    segment still headed by noise — matching `_strip_wrappers`'s `_MAX_WRAPPER_NESTING` behavior.
    A silent truncation here (the pre-fix bug) makes `argv[0]` a noise token like `"("`, which
    `_is_rg_executable` reads as "not rg" — an undetected ALLOW that also skips the fail-closed
    `_plausible_rg_pre` backstop in `main()`, since no exception was raised to reach it."""
    i = 0
    while i < len(segment) and segment[i] in _LEADING_SHELL_NOISE:
        if i >= _MAX_LEADING_SHELL_NOISE:
            raise ValueError("leading shell-noise nesting too deep")
        i += 1
    return segment[i:]


# ── rg's own value-taking flags (needed by BOTH `_strip_redirects` below and the `--pre`
# scanner further down, so defined here, ahead of both) ────────────────────────────────────────

# Every OTHER rg flag (long name, and short letter) that consumes a REQUIRED value — so that
# value is never misread as a `--pre` flag of its own (`rg -e --pre .` must NOT block: "--pre"
# there is `-e`'s VALUE, the literal search pattern, not a flag). `--pre` itself is deliberately
# excluded — it is handled as the dangerous flag, not skipped as an ordinary value flag.
# `--pre-glob` IS included (a distinct, non-dangerous value flag — see module docstring) so its
# glob value is correctly skipped rather than read as a stray positional.
# Verified against `rg --help` (ripgrep 15.1.0) — every flag rg's own help text shows taking a
# value (`NAME VALUE` / `--name=VALUE` in the synopsis), minus `--pre` itself.
_RG_LONG_VALUE_FLAGS = frozenset({
    "--regexp", "--file", "--pre-glob", "--dfa-size-limit", "--encoding", "--engine",
    "--max-count", "--regex-size-limit", "--threads", "--glob", "--iglob",
    "--ignore-file", "--max-depth", "--max-filesize", "--type", "--type-not",
    "--type-add", "--type-clear", "--after-context", "--before-context", "--color",
    "--colors", "--context", "--context-separator", "--field-context-separator",
    "--field-match-separator", "--hostname-bin", "--hyperlink-format", "--max-columns",
    "--replace", "--path-separator", "--sort", "--sortr", "--generate",
})
# The short-letter forms of the flags above that HAVE one (`-e`=`--regexp`, `-d`=`--max-depth`,
# ...). Every short value-flag in rg's help maps to a long one already in the set above.
_RG_SHORT_VALUE_LETTERS = frozenset("efEmjgdtTABCMr")


def _rg_short_cluster_consumes_next(body: str) -> bool:
    """True when a short cluster (`argv[i][1:]`) ends with an UNGLUED value letter — the value
    is the NEXT token (`-e` alone, `-in` + next for the trailing value letter). A value letter
    with anything glued after it (`-tpy` = `-t` with glued value `py`) consumes no next token;
    the FIRST value letter encountered absorbs the rest of the cluster as its glued value, same
    as clap's own short-cluster semantics (`-em` = `-e` with value `"m"`, not `-e -m`)."""
    for idx, ch in enumerate(body):
        if ch in _RG_SHORT_VALUE_LETTERS:
            return idx == len(body) - 1
    return False


def _is_rg_value_flag_before_redirect(prev: str) -> bool:
    """True when `prev` is a KNOWN rg flag that consumes the NEXT token as a required value — so
    a following `<>&`-only token is that flag's VALUE, not a real shell redirect. Needed because
    posix shlex has already stripped quotes by the time `_strip_redirects` runs: `-e '>'`
    (searching for the literal text ">") and a bare unquoted `-e >` (a real redirect after `-e`
    with no value at all — invalid rg usage, but not this hook's problem to reject) tokenize to
    the IDENTICAL `['-e', '>']`. Treating `>` as `-e`'s value here is the safe reading — it can
    only make `_strip_redirects` keep a token it might have dropped, never drop one it should
    keep, so a real trailing redirect that happens to follow one of these flags at the very end
    of a segment is simply treated as taken by the flag (over-cautious, not a bypass: it does not
    change whether `--pre` is later found in this segment's argv)."""
    if prev in _RG_LONG_VALUE_FLAGS:
        return True
    return (prev.startswith("-") and not prev.startswith("--") and len(prev) > 1
            and _rg_short_cluster_consumes_next(prev[1:]))


_REDIRECT_OPS = frozenset({">", ">>", "<", "<<", ">&", "&>", "<&", "<>"})
_FD_DIGITS_RE = re.compile(r"^\d+$")


def _looks_like_a_flag(tok: str) -> bool:
    """True when `tok` starts with `-` — a real shell redirect target is a FILE PATH, which
    never legitimately starts with a bare `-` in practice (a file named `-pre` would need `./`
    or `--` to be addressable at all); a flag-shaped token in target position is far more likely
    to be a quoted flag string that a real redirect operator (glued to it after de-quoting)
    would otherwise swallow."""
    return tok.startswith("-")


def _strip_redirects(segment: list[str]) -> list[str]:
    """Remove every shell redirection operator token together with its target and any leading
    file-descriptor digit, anywhere in the segment (not just trailing) — UNLESS EITHER:
      (a) the operator-shaped token is actually the VALUE of a preceding rg value-flag (see
          `_is_rg_value_flag_before_redirect`) — protects `rg -e '>' --pre x .` regardless of
          what follows the `>`; or
      (b) the token immediately AFTER the operator looks like a flag (`_looks_like_a_flag`) —
          protects `rg '>' --pre x .`, a BARE positional with nothing recognizable before it,
          regardless of what precedes the `>`.
    Both guards are needed: (a) catches a flag's own value being misread as an operator
    irrespective of what comes after; (b) catches an operator-shaped BARE positional (no
    preceding value-flag at all) whose "target" is actually the next real argument — a quoted
    `-e '>'`/`--regexp '>'` or a bare `rg '>' --pre x .` used to be misread as a real redirect
    and silently dropped ALONG WITH the token after it — which could be a real `--pre` — a
    bypass (P1, found in review twice: guard (a) alone was not sufficient, since a BARE
    positional has no preceding flag for it to key off). Guard (a) is ported from
    `block-no-verify`'s `_is_value_flag_before_redirect`, adapted to rg's own value-flag table;
    guard (b) has no sibling-hook precedent and is unique to rg's argument shape."""
    out: list[str] = []
    i = 0
    while i < len(segment):
        prev = segment[i - 1] if i > 0 else ""
        protected_by_flag = _is_rg_value_flag_before_redirect(prev)
        target = segment[i + 1] if i + 1 < len(segment) else None
        protected_by_target_shape = target is not None and _looks_like_a_flag(target)
        if (
            i + 1 < len(segment)
            and segment[i + 1] in _REDIRECT_OPS
            and _FD_DIGITS_RE.match(segment[i])
            and not protected_by_flag
        ):
            i += 1
            continue
        if (
            segment[i] in _REDIRECT_OPS
            and not protected_by_flag
            and not protected_by_target_shape
        ):
            i += 2
            continue
        out.append(segment[i])
        i += 1
    return out


def _split_inline_env(segment: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(segment):
        m = _INLINE_ENV.match(segment[i])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        i += 1
    return env, segment[i:]


# ── wrapper peeling (SYNC'd verbatim from block-no-verify's audited table) ─────────────────────

_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer", "command",
    "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError(
        f"operand-drop wrappers missing from _WRAPPERS: {_OPERAND_DROP_WRAPPERS - _WRAPPERS}"
    )
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError(
        f"mandatory-operand wrappers not in operand-drop: "
        f"{_MANDATORY_OPERAND_WRAPPERS - _OPERAND_DROP_WRAPPERS}"
    )
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
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError(
        f"value-flag wrappers missing from _WRAPPERS: {set(_WRAPPER_VALUE_FLAGS) - _WRAPPERS}"
    )


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _is_rg_executable(tok: str) -> bool:
    return _basename(tok) == "rg"


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    short_letters = _WRAPPER_SHORT_VALUE_LETTERS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        consumes_next = (argv[i] in value_flags
                         or (not argv[i].startswith("--")
                             and _cluster_takes_next_value(argv[i], short_letters)))
        if consumes_next and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or not _is_rg_executable(argv[i]):
            i += 1
    return argv[i:]


_ENV_SHORT_VALUE_LETTERS = _WRAPPER_SHORT_VALUE_LETTERS["env"]


def _extract_split_string(tok: str, argv: list[str], i: int) -> tuple[str | None, bool]:
    """If `tok` is an env `-S`/`--split-string` option, return (its command string,
    consumed-next-tok) — else (None, False)."""
    if tok in ("-S", "--split-string") and i + 1 < len(argv):
        return argv[i + 1], True
    if tok.startswith("--split-string="):
        return tok[len("--split-string="):], False
    if not tok.startswith("-") or tok.startswith("--"):
        return None, False
    for pos, ch in enumerate(tok[1:], start=1):
        if ch == "S":
            glued = tok[pos + 1:]
            if glued:
                return glued, False
            return (argv[i + 1], True) if i + 1 < len(argv) else (None, False)
        if ch in _ENV_SHORT_VALUE_LETTERS:
            return None, False
    return None, False


def _split_string_commands(argv: list[str]) -> tuple[list[str], list[str]]:
    env_value_flags = _WRAPPER_VALUE_FLAGS.get("env", frozenset())
    kept: list[str] = []
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        tok = argv[i]
        s_value, consumed_next = _extract_split_string(tok, argv, i)
        if s_value is not None:
            tail = argv[i + (2 if consumed_next else 1):]
            nested = " ".join([s_value, *(shlex.quote(t) for t in tail)]).rstrip()
            return [nested], kept
        takes_value = (tok in env_value_flags
                       or (not tok.startswith("--")
                           and _cluster_takes_next_value(tok, _ENV_SHORT_VALUE_LETTERS)))
        if takes_value and i + 1 < len(argv):
            kept.extend(argv[i:i + 2])
            i += 2
            continue
        kept.append(tok)
        i += 1
    return [], kept + argv[i:]


def _strip_wrappers(argv: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    env: dict[str, str] = {}
    nested: list[str] = []
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            raise ValueError("wrapper nesting too deep")
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        if wrapper == "env":
            split_cmds, argv = _split_string_commands(argv)
            nested.extend(split_cmds)
        argv = _skip_wrapper_args(wrapper, argv)
        assigned, argv = _split_inline_env(argv)
        env.update(assigned)
    return env, argv, nested


# ── `xargs`-wrapped `rg --pre` (SYNC'd from pkill-guard's xargs-target resolution, adapted) ────

# Value-taking xargs flags this hook is CONFIDENT consume a separate next token (GNU + BSD/macOS
# common ground). Boolean flags this hook is confident take no value at all. Ported verbatim from
# pkill-guard's own audited table.
_XARGS_VALUE_FLAGS = frozenset({
    "-I", "-i", "-J", "-L", "-l", "-n", "-P", "-R", "-s", "-a", "-d", "--delimiter",
})
_XARGS_BOOL_FLAGS = frozenset({
    "-0", "--null", "-o", "--open-tty", "-p", "--interactive", "-r", "--no-run-if-empty",
    "-t", "--verbose", "-x", "--exit",
})


def _xargs_wrapped_rg_args(argv: list[str]) -> list[str] | None:
    """If `argv` is an `xargs [flags] rg [rg-args...]` invocation (resolving the wrapped command
    through the SAME wrapper table as a top-level stage — `xargs env rg --pre x`, `xargs sudo rg
    --pre x`), return rg's own literal args (everything after `rg`); else None.

    `xargs` appends the piped-in items as FURTHER trailing args at runtime, which this hook never
    sees — but `--pre` would be typed literally in the shell command if present at all, so the
    literal args already recovered here are sufficient; nothing is lost by not simulating the
    runtime append. If a flag is hit that isn't confidently known as value-taking vs. boolean,
    this falls back to scanning the REST of argv for a bare (non-flag) `rg`/path-to-rg token —
    the safe direction (can only classify MORE xargs invocations as rg-wrapping, never fewer),
    same posture as pkill-guard's identical fallback for `kill`."""
    if not argv or _basename(argv[0]) != "xargs":
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            if a in _XARGS_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            if a in _XARGS_BOOL_FLAGS:
                i += 1
                continue
            for j in range(i + 1, len(argv)):
                if not argv[j].startswith("-") and _is_rg_executable(argv[j]):
                    return argv[j + 1:]
            return None
        break
    payload = _strip_wrappers(argv[i:])[1]
    if payload and _is_rg_executable(payload[0]):
        return payload[1:]
    return None


# ── rg `--pre` flag detection ────────────────────────────────────────────────────────────────

_UNRESOLVED_SUBSTITUTION_RE = re.compile(r"\$\(|`")


def _has_unresolved_substitution(argv: list[str]) -> bool:
    """True iff any token in a real `rg` invocation's own argv still carries `$(...)` / backtick
    command-substitution syntax. shlex only removes QUOTES — it never evaluates a substitution —
    so a token like `"$(printf -- --pre)"` tokenizes to the literal string `$(printf -- --pre)`,
    not to what the shell will actually pass to rg (`--pre`) once it runs the substitution. A
    literal-token comparison against `--pre` can never see that. Treating ANY unresolved
    substitution in an rg segment as dangerous (fail CLOSED) is deliberately blunt — it also
    catches wholly benign uses (`rg "$(get_pattern)" .`) — but the alternative is trusting text
    this hook cannot actually evaluate, which is exactly how `rg "$(printf -- --pre)" ./x .`
    bypasses a literal-match check while still running `--pre` at runtime."""
    return any(_UNRESOLVED_SUBSTITUTION_RE.search(tok) for tok in argv)


def _rg_segment_has_dangerous_pre(argv: list[str]) -> bool:
    """`argv` is `rg`'s own arguments (argv[0] == rg already stripped). True iff a real `--pre`
    or `--pre=COMMAND` flag is present before any `--` end-of-options marker, OR any token carries
    unresolved command-substitution syntax that could evaluate to `--pre` at runtime (see
    `_has_unresolved_substitution`)."""
    if _has_unresolved_substitution(argv):
        return True
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return False  # everything after is a literal positional, never a flag
        if tok == "--pre":
            # `rg --pre ''` (an EXPLICIT empty preprocessor command) is ripgrep's own no-op for
            # "disable preprocessing" — harmless, not the dangerous flag. Only a genuinely absent
            # or non-empty value is dangerous.
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt == "":
                i += 2
                continue
            return True
        if tok.startswith("--pre="):
            if tok == "--pre=":  # glued empty value — same no-op as `--pre ''`
                i += 1
                continue
            return True
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in _RG_LONG_VALUE_FLAGS and "=" not in tok and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            body = tok[1:]
            if _rg_short_cluster_consumes_next(body) and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        i += 1  # a positional (search pattern / path)
    return False


# SYNC'd concept from block-no-verify's `_export_env`/`_ENV_SETTING_BUILTINS` (adapted: this
# hook needs the export tracked FORWARD across later segments of the SAME command, not just
# flagged on the export's own segment — see `_find_dangerous_rg_pre`'s `exported` accumulator).
_ENV_SETTING_BUILTINS = frozenset({"export", "declare", "local", "typeset", "readonly"})


def _export_env(argv: list[str]) -> dict[str, str]:
    """The `VAR=value` assignments of an env-setting builtin segment (`export RIPGREP_CONFIG_
    PATH=/tmp/x.rc`), or {} when the segment is not one. Only literal `VAR=value` operands are
    collected; a bare `export FOO` (no value) sets nothing here and is ignored."""
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


def _resolve_rg_args(argv: list[str]) -> list[str] | None:
    """Return rg's own argv (argv[0] == rg already stripped) for a direct or `xargs`-wrapped rg
    invocation, else None. Shared by `_segment_is_dangerous` and the `RIPGREP_CONFIG_PATH` gate
    below — both need to know "is this segment an rg invocation at all", not just "is it a
    dangerous one"."""
    if argv and _is_rg_executable(argv[0]):
        return argv[1:]
    return _xargs_wrapped_rg_args(argv)


def _segment_is_dangerous(argv: list[str]) -> bool:
    """True iff the (wrapper-stripped) segment argv is either a direct `rg ... --pre ...`
    invocation or an `xargs ... rg ... --pre ...` invocation."""
    rg_args = _resolve_rg_args(argv)
    if rg_args is not None:
        return _rg_segment_has_dangerous_pre(rg_args)
    return False


_MAX_SPLIT_STRING_DEPTH = 8


def _find_dangerous_rg_pre(command: str, depth: int) -> bool:
    if depth > _MAX_SPLIT_STRING_DEPTH:
        raise ValueError("split-string nesting too deep")
    tokens = _tokenize(command)
    if tokens is None:
        raise ValueError("unbalanced quotes")
    # RIPGREP_CONFIG_PATH set by an EARLIER segment (`export RIPGREP_CONFIG_PATH=x; rg needle
    # .` — two segments of the SAME command string) must still reach a LATER rg invocation, not
    # just the segment it was set on (P1, found in review: the inline-only check below missed
    # this). Scoped to THIS command string only — reset per `_find_dangerous_rg_pre` call, so a
    # nested `env -S` command gets its own fresh accumulator, and nothing here claims to see an
    # export from a genuinely SEPARATE, earlier Bash tool call (out of scope — see the module
    # docstring's "Known limitations").
    exported: dict[str, str] = {}
    for raw_segment in _segments(tokens):
        segment = _strip_leading_shell_noise(_strip_redirects(raw_segment))
        pre_env, rest = _split_inline_env(segment)
        wrapper_env, argv, nested_cmds = _strip_wrappers(rest)
        for nested in nested_cmds:  # `env -S '<command>'` — re-inspect as a real command
            if _find_dangerous_rg_pre(nested, depth + 1):
                return True
        if _segment_is_dangerous(argv):
            return True
        # A `RIPGREP_CONFIG_PATH=<file> rg ...` set INLINE on this segment (a leading `VAR=val`
        # prefix, or via the `env` wrapper), OR exported by an earlier segment of this same
        # command, can inject a `--pre` flag from the referenced config file with NOTHING in the
        # command's own argv for the literal-flag scan above to see. This hook does not open and
        # parse that file (would grow it into a ripgrep config-file parser — see the module
        # docstring's "Known limitations"), so an rg invocation reached by either path fails
        # closed rather than silently trusting an unread file.
        if (
            "RIPGREP_CONFIG_PATH" in pre_env
            or "RIPGREP_CONFIG_PATH" in wrapper_env
            or "RIPGREP_CONFIG_PATH" in exported
        ):
            if _resolve_rg_args(argv) is not None:
                return True
        exported.update(_export_env(argv))
    return False


def find_dangerous_rg_pre(command: str) -> bool:
    """True iff `command` contains an `rg`/ripgrep invocation carrying a real `--pre` flag.
    Raises ValueError (caller fails CLOSED) on unbalanced quotes or pathological `env -S`
    nesting."""
    return _find_dangerous_rg_pre(command, 0)


_PLAUSIBLE_RG_PRE_RE = re.compile(r"\brg\b.*--pre\b", re.DOTALL)
_ENV_AT_CMD_HEAD = re.compile(
    r"(?:^|[;&|({\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*env\b[^\n;&|]*(?:-S\b|--split-string)"
)


def _plausible_rg_pre(command: str) -> bool:
    """Cheap raw scan used ONLY on the fail-closed path: could this command possibly BE a gated
    `rg ... --pre` invocation? Deliberately permissive (any `rg` token anywhere before any
    `--pre` token) so a genuinely unparseable real invocation still fails closed."""
    return bool(_PLAUSIBLE_RG_PRE_RE.search(command))


def _looks_obfuscated(command: str) -> bool:
    return bool(_ENV_AT_CMD_HEAD.search(command))


# ── verdict / hatch escalation ───────────────────────────────────────────────────────────────

_BLOCK_MSG = (
    "Refusing an `rg --pre <COMMAND>` invocation: `--pre` runs COMMAND as an arbitrary "
    "preprocessor on every matched file, turning the read-only search tool into an unrestricted "
    "local code-execution vector. If you need to run COMMAND, run it directly — don't route it "
    "through ripgrep. If this is a genuine need (e.g. searching inside a format rg can't read "
    "natively, like a PDF or a compressed archive, via a real preprocessor), request a one-time "
    'Telegram approval: RIG_HATCH_REQUEST_BLOCK_RG_PRE="<reason>" <command>.'
)
_UNBALANCED_QUOTE_BLOCK_MSG = (
    f"{HOOK_ID}: this command COULDN'T BE PARSED (likely unbalanced quotes), so the gate can't "
    "verify it isn't an `rg --pre` arbitrary-command-execution attempt (fail-closed -> blocked). "
    "This is your COMMAND, not a broken hook — fix the quoting."
)
_OBFUSCATION_BLOCK_MSG = (
    f"{HOOK_ID}: refusing an obfuscated `env -S`/`--split-string` command that could conceal an "
    "`rg --pre` invocation past inspection (fail-closed). Run the command without the "
    "split-string wrapper so the gate can verify it."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _gate_dangerous(cwd: str, command: str) -> int:
    context = {"hook": HOOK_ID, "kind": "rg-pre", "command": command}
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, context, cwd=cwd, command=command)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"rg --pre approved via hatch escalation ({hatch.reason})")
            emit("allow", f"approved via hatch escalation ({hatch.reason})")
            return 0
        warn(f"rg --pre hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{_BLOCK_MSG}")
        return BLOCK_EXIT_CODE
    emit("block", _BLOCK_MSG)
    return BLOCK_EXIT_CODE


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", f"{HOOK_ID}: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    cwd = str(event.get("cwd") or "")
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    try:
        dangerous = find_dangerous_rg_pre(command)
    except Exception as exc:  # noqa: BLE001 — inspection failed; decide by a cheap raw scan
        if _looks_obfuscated(command):
            warn(f"obfuscated (env -S/--split-string) command could hide rg --pre: {exc} — blocking (fail-closed)")
            emit("block", _OBFUSCATION_BLOCK_MSG)
            return BLOCK_EXIT_CODE
        if _plausible_rg_pre(command):
            warn(f"unbalanced quotes on a possible rg --pre invocation: {exc} — blocking (fail-closed)")
            emit("block", _UNBALANCED_QUOTE_BLOCK_MSG)
            return BLOCK_EXIT_CODE
        warn(f"unparseable but neither a plausible rg --pre nor obfuscation ({exc}) — allowing")
        emit("allow")
        return 0

    if dangerous:
        return _gate_dangerous(cwd, command)

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
