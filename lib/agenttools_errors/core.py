"""agenttools_errors.core — one shared error/exit-code layer for every agent-tools CLI.

WHAT THIS FILE IS
    The structured-error engine. Every ecosystem CLI (review / rig / tg / draw / 3d / task)
    wants the SAME shape for a failure: a three-part message — WHAT went wrong, WHY (the root
    cause + the offending file/context), HOW to fix it (a concrete command) — and a stable,
    per-class EXIT CODE so a calling script can branch on the failure class. Instead of each
    tool hand-rolling that (rig already grew its own ``riglib/errors.py``; the others print a
    bare ``Error: failed`` and ``exit(1)``), this is the single shared copy the roadmap's
    error-system-v2 item calls for ("one shared `errors` + `help` module, consumed by
    review/rig/tg/draw/3d/task").

HOW IT'S REACHED AT RUNTIME
    A command body raises an :class:`AgentToolError` (or a subclass that pins its exit code);
    the top-level CLI handler wraps the dispatch in :func:`guard`, which renders the error as
    the consistent 3-part block and returns the stable exit code. ``did_you_mean`` and the
    removed-slot registry build *precise* errors for the "unknown item / typo / removed slot"
    family rather than a useless "unknown (known: none)".

INVARIANTS / DESIGN
    - **Stdlib only at import time** (AGENTS.md hard rule): ``dataclasses`` / ``os`` / ``sys``
      / ``shutil``. No third-party imports, so a consumer's ``--help`` / ``--version`` stays
      fast and offline.
    - **The exit-code constants are a PUBLIC CONTRACT.** Scripts/CI branch on them; changing a
      value is a breaking change. They follow the ``structured-exit-codes`` skill: 0 success,
      1 internal/unexpected, 2 invalid-usage/config, 127 missing-dependency (shell
      convention), plus the common classes a CLI wants to distinguish (drift / unknown-item /
      missing-target / not-a-repo / network / permission). They match rig's existing numbering
      (rig 0/1/2/3/4/5/6/127) so rig can adopt this module without renumbering its contract.
    - **The exit code can't drift from the message.** Each error subclass pins its
      ``exit_code``; :func:`render` formats the block; both live here, so the formatting and
      the class are always consistent.
    - **A real bug is never swallowed.** :func:`guard` only translates ``AgentToolError``; any
      other exception propagates so its traceback stays visible (an unhandled crash is exit 1,
      distinguishable from a *diagnosed* problem at >= 2).

History: generalized from ``rig-cli/riglib/errors.py`` (error-system v2), which was itself
born from two same-day prod failures whose errors were thin and undiagnosable. This module is
that pattern, lifted into the shared lib so the fix lands once for the whole ecosystem.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, fields
from typing import Callable, Dict, Optional, Set, Tuple

# ── stable, per-class exit codes (PUBLIC CONTRACT — do not renumber) ───────────────────
EXIT_OK = 0
# 1 is reserved for an UNEXPECTED/internal failure (an unhandled exception): a caller can
# tell "the tool itself crashed" (1) from "the tool diagnosed a known problem" (>= 2).
EXIT_INTERNAL = 1
EXIT_USAGE = 2  # invalid argument / malformed config (the "usage" class, per the skill)
EXIT_DRIFT = 3  # a status/check command found drift (declared config and disk disagree)
EXIT_UNKNOWN_ITEM = 4  # config/args name something that doesn't exist (typo / removed slot)
EXIT_MISSING_TARGET = 5  # a referenced path/binary/file is gone on disk
EXIT_NOT_A_REPO = 6  # a repo-scoped command run outside a git repository
EXIT_NETWORK = 7  # a network/remote operation failed (DNS, timeout, 5xx, auth-less fetch)
EXIT_PERMISSION = 8  # a permission/auth failure (missing token, 401/403, file mode)
EXIT_MISSING_DEP = 127  # a required external tool/binary isn't installed (shell convention)

# Back-compat alias for rig's name: rig calls the usage class ``EXIT_CONFIG``. Both names
# point at the same value (2) so rig can adopt this module verbatim. ``EXIT_USAGE`` is the
# canonical, tool-agnostic spelling.
EXIT_CONFIG = EXIT_USAGE

# The full table, for ``--help`` / docs rendering: code -> (constant-name, one-line meaning).
EXIT_CODES: Dict[int, Tuple[str, str]] = {
    EXIT_OK: ("EXIT_OK", "success"),
    EXIT_INTERNAL: ("EXIT_INTERNAL", "unexpected internal failure (a bug — see the traceback)"),
    EXIT_USAGE: ("EXIT_USAGE", "invalid argument or malformed config"),
    EXIT_DRIFT: ("EXIT_DRIFT", "declared config and disk disagree (drift)"),
    EXIT_UNKNOWN_ITEM: ("EXIT_UNKNOWN_ITEM", "named item does not exist (typo / removed slot)"),
    EXIT_MISSING_TARGET: ("EXIT_MISSING_TARGET", "a referenced path/binary/file is gone"),
    EXIT_NOT_A_REPO: ("EXIT_NOT_A_REPO", "a repo-scoped command run outside a git repository"),
    EXIT_NETWORK: ("EXIT_NETWORK", "a network/remote operation failed"),
    EXIT_PERMISSION: ("EXIT_PERMISSION", "a permission or authentication failure"),
    EXIT_MISSING_DEP: ("EXIT_MISSING_DEP", "a required external tool/binary isn't installed"),
}


# ── the structured error ──────────────────────────────────────────────────────────────
def _reconstruct_error(cls, what):
    """Rebuild an :class:`AgentToolError` subclass during unpickle (pickle then applies state).

    Called with just ``(cls, what)`` so the constructor is happy; pickle's state dict (the full
    why/fix/install/exit_code payload) is applied to ``__dict__`` immediately afterwards by the
    pickle protocol. See :meth:`AgentToolError.__reduce__`.
    """
    return cls(what)


# ``eq=False`` keeps Exception's data-model semantics: identity-based ``__eq__`` and a working
# ``__hash__`` (a bare ``@dataclass`` would set ``__hash__ = None``, making errors unhashable and
# value-comparable — a surprising regression from a normal Exception). Subclasses inherit this.
@dataclass(eq=False)
class AgentToolError(Exception):
    """A structured, renderable error: WHAT happened / WHY / HOW to fix + an exit code.

    ``what`` — the symptom, one line ("unknown mcp item: reviewr").
    ``why``  — the root cause + context: the offending CONFIG FILE PATH + key when relevant.
    ``fix``  — a concrete command or edit the user can run/make right now.
    ``install`` — when the failure is a missing dependency, the exact install command. It is
                  rendered on its own ``install:`` line (the ``structured-exit-codes`` skill's
                  fourth field) so a human — and a bootstrap script — sees how to get it.
    ``exit_code`` — the failure class (one of the EXIT_* constants); subclasses pin it.

    The base class defaults to ``EXIT_INTERNAL``; prefer a specific subclass (``UsageError``,
    ``MissingDepError``, …) so the exit code matches the class. ``str(e)`` is the WHAT line, so
    a bare ``print(e)`` / log line stays terse.
    """

    what: str
    why: str = ""
    fix: str = ""
    install: str = ""
    exit_code: int = EXIT_INTERNAL

    def __post_init__(self) -> None:
        # Exception's own machinery wants args set; keep str(e) == the WHAT line.
        super().__init__(self.what)

    def __str__(self) -> str:
        return self.what

    def __reduce__(self):
        # Exception.__reduce__ pickles only ``args`` (== (what,)), which would drop why/fix/
        # install/exit_code across a process boundary (multiprocessing / ProcessPoolExecutor /
        # any queue that pickles exceptions). Restore the full structured payload on unpickle so
        # a supervised child's error round-trips intact. Introspect ``dataclasses.fields`` so a
        # subclass that adds a field has it preserved too (not a hardcoded five). The state dict
        # is applied to __dict__ by the pickle protocol after _reconstruct_error builds the shell.
        state = {f.name: getattr(self, f.name) for f in fields(self)}
        return (_reconstruct_error, (self.__class__, self.what), state)


@dataclass(eq=False)
class UsageError(AgentToolError):
    """Invalid argument or malformed config — a bad value, type, or unknown flag. Exit 2."""

    exit_code: int = EXIT_USAGE


# rig spells this ``ConfigError``; keep the alias name available for a drop-in adoption.
@dataclass(eq=False)
class ConfigError(UsageError):
    """Malformed/invalid config (rig's spelling of the usage class). Exit 2."""


@dataclass(eq=False)
class DriftError(AgentToolError):
    """A status/check command found drift (config and disk disagree). Exit 3."""

    exit_code: int = EXIT_DRIFT


@dataclass(eq=False)
class UnknownItemError(AgentToolError):
    """An item named in config/args doesn't exist (typo or a removed slot). Exit 4."""

    exit_code: int = EXIT_UNKNOWN_ITEM


@dataclass(eq=False)
class MissingTargetError(AgentToolError):
    """A referenced path/binary/file is gone on disk (a dead hook path, …). Exit 5."""

    exit_code: int = EXIT_MISSING_TARGET


@dataclass(eq=False)
class NotARepoError(AgentToolError):
    """A repo-scoped command was run outside a git repository. Exit 6."""

    exit_code: int = EXIT_NOT_A_REPO


@dataclass(eq=False)
class NetworkError(AgentToolError):
    """A network/remote operation failed (DNS, timeout, 5xx). Exit 7."""

    exit_code: int = EXIT_NETWORK


@dataclass(eq=False)
class PermissionDeniedError(AgentToolError):
    """A permission/auth failure (missing token, 401/403, file mode). Exit 8.

    Named ``PermissionDeniedError`` (not ``PermissionError``) so the builtin ``PermissionError``
    stays usable at a caller's catch-site; the distinct name also means ``repr``/tracebacks/Sentry
    show ``PermissionDeniedError``, matching the public contract.
    """

    exit_code: int = EXIT_PERMISSION


@dataclass(eq=False)
class MissingDepError(AgentToolError):
    """A required external tool/binary isn't installed. Exit 127 (shell convention)."""

    exit_code: int = EXIT_MISSING_DEP


# ── color + rendering ───────────────────────────────────────────────────────────────
def _c(code: str, s: str, color: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if color else s


# Terminal-dangerous chars to strip from user-controlled error fields before printing, so a bad
# config VALUE (a path, an item name, a cwd) can't hijack or SPOOF the rendered block:
#   - C0 controls 0x00–0x1F except TAB (raw ESC / CR / screen-clear / BEL),
#   - DEL 0x7F,
#   - C1 controls 0x80–0x9F (notably 0x9B CSI, an ESC-`[` equivalent on some terminals),
#   - Unicode bidi overrides / isolates + zero-width + line/para separators (U+200B–U+200F,
#     U+202A–U+202E, U+2066–U+2069, U+2028/U+2029, U+FEFF) — these can visually reorder or hide
#     text so a malicious value masquerades as a safe one.
# Our own color codes are added AFTER sanitizing, so they survive.
_CONTROL_CODEPOINTS = (
    [c for c in range(0x20) if c != 0x09]  # C0 minus TAB
    + [0x7F]  # DEL
    + list(range(0x80, 0xA0))  # C1
    + [0x200B, 0x200C, 0x200D, 0x200E, 0x200F]  # zero-width + LRM/RLM
    + [0x202A, 0x202B, 0x202C, 0x202D, 0x202E]  # bidi embeddings/overrides
    + [0x2066, 0x2067, 0x2068, 0x2069]  # bidi isolates
    + [0x2028, 0x2029]  # line / paragraph separator
    + [0xFEFF]  # BOM / zero-width no-break space
)
_CONTROL_TABLE = {cp: None for cp in _CONTROL_CODEPOINTS}


def _sanitize(s: str) -> str:
    """Strip terminal-dangerous control/format chars (keep TAB) from a user-controlled field.

    Defangs ANSI/CSI injection and bidi/zero-width spoofing in a value that reaches an error's
    ``what``/``why``/``fix``/``install`` before it's printed. See ``_CONTROL_CODEPOINTS``.
    """
    return s.translate(_CONTROL_TABLE)


def render(err: AgentToolError, *, color: bool = True) -> str:
    """Render an :class:`AgentToolError` as the consistent block (what / why / fix / install).

    Always shows the WHAT (prefixed ``error:``, red); shows WHY (dim label), FIX (green
    label), and INSTALL (yellow label) only when populated, so a terse error doesn't print
    empty labels. The label words always appear when their field is set — the contract the CLI
    handler and tests rely on. Each field is sanitized of control chars first (see
    :func:`_sanitize`) so a malicious config value can't emit raw terminal escapes.
    """
    lines = [_c("31", f"error: {_sanitize(err.what)}", color)]
    if err.why:
        lines.append(_c("2", "  why: ", color) + _sanitize(err.why))
    if err.fix:
        lines.append(_c("32", "  fix: ", color) + _sanitize(err.fix))
    if err.install:
        lines.append(_c("33", "  install: ", color) + _sanitize(err.install))
    return "\n".join(lines)


def _stream_is_tty(stream: object) -> bool:
    """True iff ``stream`` is a real TTY (best-effort; a faked stream without isatty is not)."""
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except Exception:
        return False


# Values that mean "off" for FORCE_COLOR — matches the FORCE_COLOR=0 convention used by
# Node-style CLIs (chalk, supports-color). Must mirror agenttools_help's identical set so the
# two modules' color decisions never drift.
_FORCE_COLOR_OFF = {"", "0", "false", "no", "off"}


def should_color(stream: object = None) -> bool:
    """Whether to emit ANSI color for ``stream`` (default stderr).

    ``NO_COLOR`` (any non-empty value) disables — the no-color.org standard (an *empty*
    ``NO_COLOR=""`` does NOT disable). ``FORCE_COLOR`` forces color on for a truthy value
    (``1``/``true``/…) even when piped; ``FORCE_COLOR=0`` (or ``false``/``no``/``off``) forces it
    OFF. Otherwise color only on a real TTY. The same precedence the help formatter uses, kept
    here so an error printed before any help is colorized identically.
    """
    if os.environ.get("NO_COLOR"):
        return False
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force.strip().lower() not in _FORCE_COLOR_OFF
    return _stream_is_tty(stream if stream is not None else sys.stderr)


def guard(fn: Callable[[], int], *, stream: object = None) -> int:
    """Run ``fn`` and translate any :class:`AgentToolError` into render() + its exit code.

    The single top-level CLI handler: a command body raises a structured error and this turns
    it into a consistent printed block (on stderr by default) + the stable per-class exit code.
    A non-:class:`AgentToolError` (a real bug) is NOT swallowed — it propagates so the stack
    trace is visible (and the process exits 1 by Python's own machinery).

    A command body that forgets ``return`` (falls off the end → ``fn()`` is ``None``) is
    normalized to :data:`EXIT_INTERNAL`, NOT silently reported as success — a missing return is a
    bug, not a 0. Any non-int result is treated the same way.
    """
    out = stream if stream is not None else sys.stderr
    try:
        rc = fn()
    except AgentToolError as exc:
        print(render(exc, color=should_color(out)), file=out)
        return exc.exit_code
    # ``type(rc) is int`` (not isinstance) so a bool — bool is a subclass of int — is also caught
    # as "not a real exit code" and normalized to EXIT_INTERNAL, matching the contract that only a
    # genuine int return is honored.
    return rc if type(rc) is int else EXIT_INTERNAL


# ── did-you-mean (Levenshtein) ──────────────────────────────────────────────────────
def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance, stdlib-only (small strings — item/command names — so O(n*m))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def did_you_mean(bad: str, candidates: Set[str]) -> Optional[str]:
    """The nearest candidate to ``bad`` within a sensible edit-distance threshold, else None.

    Returns ``None`` when the candidate set is empty or nothing is close enough — so we never
    fabricate a bogus suggestion for a wildly different token. The threshold scales with the
    longer string's length (a longer name tolerates more edits) but is capped at 3, so
    "zzzzzzzz" → "review" is rejected. Ties break alphabetically for determinism.

    A length prefilter (``abs(len(bad) - len(c)) > 3`` ⇒ skip; the cap is 3) avoids the O(n·m)
    edit-distance for candidates that can't possibly match, so a large catalog (a model registry,
    MCP item list) stays cheap. Each candidate's distance is computed at most once.

    Tokens (and candidates) shorter than 2 chars are skipped — every length-1 string is within
    edit distance 1 of every other length-1 string, so "did you mean `a`?" for "ab" is noise, not
    help (and an empty ``bad`` has no meaningful suggestion at all).
    """
    if len(bad) < 2 or not candidates:
        return None
    best: Optional[str] = None
    best_key: Tuple[int, str] = (10**9, "")
    for c in candidates:
        if len(c) < 2:
            continue  # a 1-char candidate is too short to be a meaningful suggestion
        if abs(len(bad) - len(c)) > 3:
            continue  # can't be within the capped threshold; skip the full computation
        dist = _levenshtein(bad, c)
        threshold = min(3, max(1, round(0.4 * max(len(bad), len(c)))))
        if dist > threshold:
            continue
        key = (dist, c)  # nearest wins; ties break alphabetically (deterministic)
        if key < best_key:
            best_key, best = key, c
    return best


# ── removed / deprecated slot registry ──────────────────────────────────────────────
@dataclass(frozen=True)
class RemovedSlot:
    """A slot that USED to exist and was removed — so its config key/arg is now invalid.

    ``reason`` names WHY/WHEN it went (the PR + the rationale); the error builder turns it
    into a precise "remove ``<key>`` from ``<config path>``" fix instead of a useless
    "unknown item (known: none)". Each tool seeds its own registry — this module supplies the
    type + lookup, not a shared global table (a removed rig MCP slot is meaningless to draw).
    """

    category: str
    name: str
    reason: str


class RemovedSlotRegistry:
    """A per-tool registry of removed slots, keyed (category, name).

    A tool builds one and seeds it whenever it removes a catalog slot, so a lingering config
    reference explains itself (cites the removal PR + the fix) instead of looking like a typo.
    """

    def __init__(self) -> None:
        self._slots: Dict[Tuple[str, str], RemovedSlot] = {}

    def add(self, slot: RemovedSlot) -> "RemovedSlotRegistry":
        """Register a removed slot; returns self so seeding can chain."""
        self._slots[(slot.category, slot.name)] = slot
        return self

    def lookup(self, category: str, name: str) -> Optional[RemovedSlot]:
        """The removed slot for ``(category, name)``, or ``None`` if it was never a slot."""
        return self._slots.get((category, name))


# ── error builders (the heuristics, assembled into structured errors) ───────────────
def unknown_item_error(
    *,
    category: str,
    bad: str,
    known: Set[str],
    config_path: str = "",
    key: str = "",
    removed: Optional[RemovedSlotRegistry] = None,
) -> UnknownItemError:
    """Build the error for config/args that name a non-existent item.

    Priority of explanation (most specific first):
      1. **removed slot** — the name was a real slot that got removed: cite the PR + tell the
         user to remove ``<key>`` (or stop passing ``<bad>``).
      2. **empty set** — there are NO known items in this category: say so plainly, don't
         print "known: none".
      3. **did-you-mean** — one known item is close: suggest it.
      4. **fallthrough** — list the known names so the user can pick a valid one.

    ``key`` (a dotted config key) and ``config_path`` (the offending file) are optional; when
    given they appear in the WHY/FIX so the user knows exactly where to look. When absent (an
    arg, not a config key) the messages degrade gracefully to "you passed ``<bad>``".

    ``bad`` and ``category`` must be non-blank — there's no coherent "unknown item: <nothing>"
    error (a blank value is a caller bug, not a user typo).
    """
    if not bad or not bad.strip():
        raise ValueError("unknown_item_error: bad must be a non-blank item name")
    if not category or not category.strip():
        raise ValueError("unknown_item_error: category must be a non-blank string")
    where = f" (declared in {config_path})" if config_path else ""
    # Subject of the WHY sentence: the config KEY when there is one, else the bad name itself —
    # so the sentence always has a grammatical subject ("`<key>` names…" / "`<bad>` names…").
    keyref = f"`{key}` " if key else f"`{bad}` "
    fix_loc = f" in {config_path}" if config_path else ""

    if removed is not None:
        slot = removed.lookup(category, bad)
        if slot is not None:
            return UnknownItemError(
                what=f"removed {category} slot: {bad}",
                why=f"{keyref}({slot.reason}){where}".strip(),
                fix=(
                    f"remove `{key}`{fix_loc}"
                    if key
                    else f"stop passing `{bad}` — that {category} slot was removed"
                ),
            )

    if not known:
        return UnknownItemError(
            what=f"unknown {category} item: {bad}",
            why=f"there are no {category} items{where}".strip(),
            fix=(
                f"remove the `{category}` block{fix_loc}"
                if config_path
                else f"there are no {category} items to choose from"
            ),
        )

    suggestion = did_you_mean(bad, known)
    if suggestion is not None:
        # Connect the suggestion to the action: tell the user to change the bad name TO the
        # suggestion (not just "fix `<key>`", which leaves them guessing the target value).
        fix = f"did you mean `{suggestion}`?"
        if key:
            fix += f" change `{key}` to `{suggestion}`{fix_loc}"
        else:
            fix += f" use `{suggestion}` instead of `{bad}`"
        return UnknownItemError(
            what=f"unknown {category} item: {bad}",
            why=f"{keyref}names an item not in the {category} catalog{where}".strip(),
            fix=fix,
        )

    known_list = ", ".join(sorted(known))
    return UnknownItemError(
        what=f"unknown {category} item: {bad}",
        why=f"{keyref}names an item not in the {category} catalog{where}".strip(),
        fix=f"use one of: {known_list}"
        + (f" — or remove `{key}`{fix_loc}" if key else ""),
    )


def missing_dep_error(
    *,
    dep: str,
    needed_for: str,
    install: str,
    rerun: str = "",
) -> MissingDepError:
    """Build the error for a required external tool/binary that isn't installed (exit 127).

    ``dep`` — the missing binary ("openscad"); ``needed_for`` — what it's needed for ("to
    produce the mesh"); ``install`` — the exact install command ("brew install openscad");
    ``rerun`` — optionally, the command to re-run after installing. The install command lands
    on its own ``install:`` line so a bootstrap can pick it up. ``dep`` must be non-blank — a
    blank one would render "error: `` is not installed".
    """
    if not dep or not dep.strip():
        raise ValueError("missing_dep_error: dep must be a non-blank binary name")
    fix = f"install it, then re-run: {rerun}" if rerun else "install it, then re-run the command"
    return MissingDepError(
        what=f"`{dep}` is not installed",
        why=f"this command needs {dep} {needed_for}".strip(),
        fix=fix,
        install=install,
    )


def require_tool(
    dep: str,
    *,
    needed_for: str,
    install: str,
    rerun: str = "",
) -> str:
    """Return the path to ``dep`` on PATH, or raise a :class:`MissingDepError` (exit 127).

    The one-call guard for "this command shells out to an external tool": resolves the binary
    with :func:`shutil.which`; on a miss it raises the structured missing-dependency error with
    the install command, so :func:`guard` prints the actionable block and exits 127.
    """
    if not dep:
        raise ValueError("require_tool: dep must be a non-empty binary name")
    path = shutil.which(dep)
    if path is None:
        raise missing_dep_error(dep=dep, needed_for=needed_for, install=install, rerun=rerun)
    return path


def missing_target_error(
    *,
    what_kind: str,
    target: str,
    why: str,
    regen: str,
) -> MissingTargetError:
    """Build the error for a config/arg that points at a path/binary that's gone on disk.

    ``what_kind`` is a short noun ("hook", "binary", "skill"); ``target`` the missing path;
    ``why`` the root cause; ``regen`` how to recreate it (a concrete command).
    """
    return MissingTargetError(
        what=f"missing {what_kind}: {target}",
        why=why,
        fix=regen,
    )


def not_a_repo_error(*, command: str, cwd: str = "") -> NotARepoError:
    """Build the error for a repo-scoped command run outside a git repository (exit 6).

    Per the roadmap's "commands should work outside a repo" principle, only repo-bound ACTIONS
    raise this; read/list/global commands should run anywhere. ``cwd`` (optional) names where
    we looked.
    """
    where = f" ({cwd} is not inside a git repo)" if cwd else ""
    return NotARepoError(
        what=f"`{command}` must run inside a git repository",
        why=f"no git repository found{where}".strip(),
        fix="cd into a repository (or run `git init` here) and re-run",
    )


__all__ = [
    # exit codes
    "EXIT_OK",
    "EXIT_INTERNAL",
    "EXIT_USAGE",
    "EXIT_CONFIG",
    "EXIT_DRIFT",
    "EXIT_UNKNOWN_ITEM",
    "EXIT_MISSING_TARGET",
    "EXIT_NOT_A_REPO",
    "EXIT_NETWORK",
    "EXIT_PERMISSION",
    "EXIT_MISSING_DEP",
    "EXIT_CODES",
    # error types
    "AgentToolError",
    "UsageError",
    "ConfigError",
    "DriftError",
    "UnknownItemError",
    "MissingTargetError",
    "NotARepoError",
    "NetworkError",
    "PermissionDeniedError",
    "MissingDepError",
    # rendering + handler
    "render",
    "guard",
    "should_color",
    # heuristics
    "did_you_mean",
    "RemovedSlot",
    "RemovedSlotRegistry",
    "unknown_item_error",
    "missing_dep_error",
    "require_tool",
    "missing_target_error",
    "not_a_repo_error",
]
