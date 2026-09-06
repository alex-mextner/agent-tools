#!/usr/bin/env python3
"""agents-hooks/v1 stop hook — block a reply that contains a banned word (agent-tools#548).

Alex maintains a machine-readable banned-word list at ~/.claude/DICT.json (stem-based
regexes, not a word-form list) and it is enforced today for outgoing Telegram messages via
`tg` (tg-cli#302). That enforcement has ZERO effect on text Claude Code types directly into
its own chat UI with Alex — a completely separate, previously unguarded surface. This hook
closes that gap: it reads the assistant's own just-finished reply from the session
transcript, checks it against the SAME dictionary, and BLOCKS the Stop (exit 10) when a
banned word is found, telling the model exactly what matched and what to say instead.

Cyrillic scoping (matches DICT.json's own stated semantics and tg-cli's implementation,
see tg-cli's features/cli/dict-gate.ts `checkDictionary`): whether the whole current-turn
assistant text contains ANY Cyrillic codepoint (U+0400-U+04FF) is judged ONCE, as one
string. A rule with `only_if_cyrillic: true` (e.g. `fork-to-alex`, `land-landing-in-
russian`) applies only when that whole-turn check is true. This is why a subagent's plain
English report using the ordinary word "fork" is never touched by `fork-to-alex` — no
Cyrillic in the turn, so that rule never even runs.

Cooldown vs. loop guard — these are different mechanisms for a different failure mode.
Unlike stop-completion-selfcheck (which blocks at most once per TTL as a one-time
reminder), THIS hook re-checks and blocks on EVERY Stop that still contains a real
violation: there is no cooldown suppressing the block/allow decision, because blocking an
already-clean turn costs nothing and a violation must never be allowed to just age out.
What this hook DOES need is a LOOP GUARD: a per-session count of CONSECUTIVE
violating Stops, capped (default 3, CHAT_DICT_GATE_LOOP_GUARD_CAP) so a model that can't
converge on clean wording can't wedge the whole interactive session in a block-forever
loop. Crossing the cap allows the stop anyway, logged under its own decision string
(`allow_loop_guard_cap`) rather than a plain allow/block, and resets the streak counter (a
later clean turn ALSO resets it — the cap is per unbroken streak, not a lifetime count).

Retry-boundary scanning (the empirical reason the loop guard is not just decorative):
duplicating stop-completion-selfcheck's reverse-scan-since-last-real-user-message logic
verbatim would NOT be safe here. A real Stop block injects a synthetic transcript record —
verified against live sessions (e.g. ~/.claude/projects/*/*.jsonl, a
`hook_blocking_error`/`stop_hook_summary` pair) — shaped as `type:"user"`, `isMeta:true`,
`message.content` a PLAIN STRING starting with "Stop hook feedback:\n...". That record is
NOT a genuine user turn boundary (isMeta is true, so stop-completion-selfcheck's own
`_is_real_user_turn_boundary` correctly treats it as turn-continuation, not a boundary) —
but for THIS hook, treating it as continuation would mean a corrected rewrite's text is
concatenated together with the ORIGINAL violating text from before the block, forever, so
the concatenated string can never look clean and the gate could never let a fixed turn
through — the loop guard cap would become the ONLY exit, not a defense-in-depth backstop.
So this hook adds a second boundary kind, `_is_retry_boundary`, that also stops the reverse
scan: text after the most recent Stop-hook-feedback record (real or synthetic-from-any-
stop-point-hook) is judged as this attempt's own text, independent of whatever came before.

Fail-open on a broken dictionary is a DELIBERATE divergence from tg-cli (which fails
CLOSED — refuses to send — on a malformed file, an invalid rule, or an uncompilable
regex). tg's failure mode is "refuse one Telegram send": cheap, instantly retryable,
nothing else is blocked by it. This hook's failure mode is "the user cannot end their
interactive CLI turn": a config typo trapping an entire session is strictly worse than the
banned word occasionally slipping through, so `on_error: "open"` governs every dictionary-
lifecycle failure here, not just process crashes — see `load_rules`.

Contract (agents-hooks/v1):
  stdin  : JSON event; a session/turn id in event.event_id (or args.session_id); an
           optional args.transcript_path (CC-forwarded) pointing at the turn's JSONL log
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash, a missing/broken dictionary, or an unreadable transcript must
never trap the agent unable to stop.

NOTE ON DUPLICATION (agent-tools AGENTS.md convention: each agent-hooks/<name>/ directory
is a self-contained deployable unit): the transcript tail-read and reverse-scan logic below
is intentionally DUPLICATED from, not imported from,
agent-hooks/stop-completion-selfcheck/stop_selfcheck.py — trimmed to the subset this hook
needs (assistant TEXT only; no had_tool_use tracking) and extended with the retry-boundary
kind described above. See that file for the original and its own extensive comments on the
byte-bounded tail read and the CC transcript record shapes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
# The only DICT.json format version this hook understands — mirrors tg-cli's own
# SUPPORTED_DICTIONARY_VERSION (features/cli/dict-gate.ts). A dictionary declaring a
# different version is treated as a lifecycle failure (fail-open with a warning), not
# silently parsed under possibly-changed rule semantics.
SUPPORTED_DICTIONARY_VERSION = 1

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "CHAT_DICT_GATE_MARKER_DIR", "~/.cache/agent-tools/chat-dict-gate")))
FIRINGS_LOG = Path(os.path.expanduser(os.environ.get(
    "CHAT_DICT_GATE_FIRINGS_LOG", str(MARKER_DIR / "firings.jsonl"))))
DEFAULT_DICT_PATH = os.path.expanduser(os.environ.get(
    "CHAT_DICT_GATE_DICT_PATH_DEFAULT", "~/.claude/DICT.json"))
# An explicit override (distinct from the default above): a missing DEFAULT path silently
# disables the gate (a machine without the file is not an error, mirrors tg); a missing
# EXPLICIT override still fails open (allow) per this hook's on_error contract, but WARNS,
# because naming a path that doesn't exist is a real misconfiguration worth surfacing.
DICT_PATH_OVERRIDE = os.environ.get("CHAT_DICT_GATE_DICT_PATH", "")
# How many CONSECUTIVE violating Stops (not a lifetime count) are tolerated before this
# hook gives up and allows anyway, to guarantee it can never wedge a session forever.
# Clamped to >= 1: a cap of 0 would silently never block at all, defeating the hook.


def _env_int(name: str, default: int) -> int:
    """An env knob parsed at import time must not be able to crash the process BEFORE
    `main()`'s fail-open handling exists (a review finding): `int("abc")` here would exit
    with a traceback and no protocol JSON. A non-integer value falls back to the default."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


LOOP_GUARD_CAP = max(1, _env_int("CHAT_DICT_GATE_LOOP_GUARD_CAP", 3))
# Same tail-read knobs as stop-completion-selfcheck, same defaults, own env names.
TRANSCRIPT_TAIL_LINES = _env_int("CHAT_DICT_GATE_TRANSCRIPT_TAIL_LINES", 500)
TAIL_READ_BYTES = _env_int("CHAT_DICT_GATE_TRANSCRIPT_TAIL_BYTES", 2_000_000)

# A codepoint in the Cyrillic block is sufficient evidence the turn is Russian-language
# text — no need for a full script-aware Unicode property (Python's stdlib `re` can't
# parse `\p{}` anyway; this range covers the alphabet used throughout DICT.json's rules).
_CYRILLIC_RE = re.compile("[Ѐ-ӿ]")
_RETRY_BOUNDARY_PREFIX = "Stop hook feedback:"


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"chat-dictionary-gate: {msg}\n")


def session_id(event: dict) -> str:
    """A hash stable across every Stop within one CC session — the loop guard's counter
    is keyed on it, so instability here would silently defeat the cap (a fresh id per
    invocation would mean `_read_counter` always sees 0, `new_count` is always 1, and the
    cap can never trip). Verified against `lib/cc_hook_bridge/dispatch.py`'s
    `to_v1_event`: the v1 event's TOP-LEVEL `event_id` is set to
    `cc_event.get("session_id", "")` — CC's own session id, constant for the life of the
    session, not a per-invocation value — and `args["session_id"]` (when CC forwards it)
    carries the identical value. Both precedence branches below therefore resolve to the
    same stable id in real deployments; `stop-completion-selfcheck`'s cooldown relies on
    the identical lookup and precedence and is proven to work in production.
    """
    args = event.get("args") or {}
    raw = (
        args.get("session_id")
        or event.get("event_id")
        or event.get("cwd")
        or "default"
    )
    return hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def _disabled() -> bool:
    """Kill switch: an env flag or a sentinel file, checked before any marker/log write.

    Reads the env var live (not the ``DISABLE_ENV`` module constant captured at import
    time) so a test — or a caller — that sets it right before invoking the hook takes
    effect; `stop_selfcheck.py`'s sibling constant has the same latent staleness, it just
    isn't exercised by a test that sets the env var after import.
    """
    if os.environ.get("CHAT_DICT_GATE_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return (MARKER_DIR / "DISABLED").exists()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Per-session streak counter (the loop guard)
# ---------------------------------------------------------------------------


def _counter_file(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.json"


def _read_counter(sid: str) -> int:
    """A hand-edited or corrupted counter file must never be able to defeat the loop
    guard. Two shapes need explicit handling beyond a plain `int(...)` cast (a review
    finding): a NEGATIVE value (this hook never writes one, but nothing stops a human or a
    bug elsewhere from doing so) would count DOWN toward the cap on every future violation
    instead of up, taking roughly `abs(value)` extra blocks to ever trip the guard — a
    silent, much-weakened cap rather than an outright bypass, but still not "0" as the
    corrupt-counter contract promises; and a non-finite value (`1e999` parses to
    `float("inf")`) makes `int(...)` raise `OverflowError`, which used to be uncaught.
    Both now resolve to 0, matching every other corrupt shape."""
    try:
        data = json.loads(_counter_file(sid).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            # A top-level JSON scalar/list/null (`[]`, `null`, `5`) has no `.get` — an
            # AttributeError, not the TypeError a non-numeric field would raise. Checking
            # this explicitly (a review finding) rather than folding it into the except
            # tuple keeps the docstring-level contract ("corrupt counter -> treat as 0")
            # actually true for every corrupt shape, not just some of them.
            return 0
        raw = data.get("consecutive_blocks", 0)
        if not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return 0
        value = int(raw)
        return value if value >= 0 else 0
    except (OSError, ValueError, TypeError, OverflowError):
        # ValueError also catches json.JSONDecodeError (a ValueError subclass); TypeError
        # catches other unexpected shapes; OverflowError is defense-in-depth alongside the
        # explicit math.isfinite check above (kept in case a future edit removes that
        # check without noticing this is the safety net behind it).
        return 0


def _write_counter(sid: str, n: int) -> bool:
    """Persist the loop-guard counter. Returns False on any failure so the CALLER can
    decide how to fail open (see `_decide`) — this function must never decide that on its
    own, since "block anyway" vs. "give up and allow" depends on which decision the write
    was for. Writes via a temp-file-then-rename so a process killed mid-write can never
    leave a half-written, corrupt counter file for the next invocation to trip over
    (`_read_counter` already treats a corrupt file as 0, but atomicity avoids relying on
    that safety net for an otherwise-avoidable failure mode)."""
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _counter_file(sid).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"consecutive_blocks": n}), encoding="utf-8")
        os.replace(tmp, _counter_file(sid))
        return True
    except OSError as exc:
        warn(f"could not write loop-guard counter for session {sid}: {exc}")
        return False


def _log_firing(sid: str, decision: str, rule_ids: list[str], hook_ms: float) -> None:
    """Append one JSONL row per invocation that reaches a decision — write-only, mirrors
    agent-tools#529's convention; this hook never reads the log back."""
    row = {"ts": time.time(), "session": sid, "decision": decision, "rule_ids": rule_ids,
           "hook_ms": round(hook_ms, 2)}
    try:
        FIRINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRINGS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


# ---------------------------------------------------------------------------
# Transcript reading (duplicated + trimmed + extended subset of stop_selfcheck.py)
# ---------------------------------------------------------------------------


def _read_tail_records(path: str, max_lines: int) -> list[dict]:
    """The last ``max_lines`` parseable JSON objects from a JSONL transcript, read from a
    byte-bounded window at the end of the file. See stop_selfcheck.py's docstring on this
    function for the full rationale (bounded I/O regardless of file size, the NEL/U+2028
    splitting hazard, the leading-partial-line trim). Best-effort: a bad line is skipped.
    """
    if max_lines <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        read_size = max(0, min(size, TAIL_READ_BYTES))
        fh.seek(size - read_size)
        chunk = fh.read(read_size)
    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")  # NOT splitlines() — see stop_selfcheck.py for why
    if read_size < size and lines:
        lines = lines[1:]  # drop a line our own seek may have cut mid-record
    lines = lines[-max_lines:]
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _message_content(record: dict) -> str | list | None:
    message = record.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return None


def _is_real_user_turn_boundary(record: dict, content) -> bool:
    """True for an actual human message; False for a synthetic/injected "user" record.
    See stop_selfcheck.py's docstring for the full rationale (tool_result-shaped user
    records, isMeta/isSidechain, a mixed tool_result+text record)."""
    if record.get("isMeta") or record.get("isSidechain"):
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
            for b in content
        )
    return False


def _is_retry_boundary(record: dict, content) -> bool:
    """True for the synthetic record a Stop-hook block injects (this hook's OWN block, or
    a sibling stop-point hook's — the shape is CC's, not specific to any one hook): a
    `type:"user"`, `isMeta:true` record whose string content starts with "Stop hook
    feedback:". Verified against live transcripts (see module docstring). Text after this
    record is the CURRENT rewrite attempt; text before it was already judged (by this or
    another hook) at a previous Stop and must not be re-scanned forever."""
    if record.get("type") != "user" or not record.get("isMeta"):
        return False
    return isinstance(content, str) and content.startswith(_RETRY_BOUNDARY_PREFIX)


def _assistant_text_blocks(content) -> list[str]:
    """Only `type:"text"` blocks (or a bare string) — `thinking`/`tool_use` blocks are not
    the chat surface DICT.json governs and must not be scanned."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
    return []


def _transcript_path(event: dict) -> str:
    args = event.get("args")
    args = args if isinstance(args, dict) else {}
    return args.get("transcript_path") or event.get("transcript_path") or ""


def _extract_current_turn_text(transcript_path: str) -> str:
    """Assistant text since the last real user message OR the last retry boundary,
    whichever is more recent. Returns "" on any read/parse trouble or an empty turn — that
    naturally resolves to zero rule hits (fail-open by construction, no special-casing
    needed at the call site). If the reverse scan runs off the front of the scanned window
    without finding either boundary, whatever WAS collected is still used (unlike
    stop-completion-selfcheck's FULL fallback): scanning a superset of the true turn can
    only find MORE potential violations, never fewer, so this is the safe direction for a
    hard content gate — the loop guard is the backstop if that ever over-triggers."""
    if not transcript_path:
        return ""
    try:
        records = _read_tail_records(transcript_path, TRANSCRIPT_TAIL_LINES)
    except OSError:
        return ""
    # One joined string PER RECORD, collected newest-first, then the LIST OF RECORDS is
    # reversed at the end — never the flattened list of blocks. Reversing a flat
    # block-level list here would silently reverse the WITHIN-record block order too
    # (e.g. a record with text blocks [A1, A2] would come out as A2-then-A1): a real bug
    # caught in review, exercised by
    # test_multiple_text_blocks_in_one_record_keep_their_original_order.
    record_texts: list[str] = []
    for record in reversed(records):
        if record.get("isSidechain"):
            # A SUBAGENT's own transcript record inlined into the parent file (some CC
            # builds interleave sidechain traffic this way — see
            # `_is_real_user_turn_boundary`'s handling of it on user records). Skipping it
            # here too (a review finding) matters specifically for THIS hook's Cyrillic
            # scoping promise: an inlined subagent's plain-English "forked" report must
            # never be concatenated into a Cyrillic parent turn and flip
            # `has_cyrillic` true, which would fire `fork-to-alex` on text that was never
            # part of the reply Alex actually reads.
            continue
        rtype = record.get("type")
        content = _message_content(record)
        if rtype == "user":
            if _is_real_user_turn_boundary(record, content) or _is_retry_boundary(record, content):
                break
            continue
        if rtype != "assistant":
            continue
        blocks = _assistant_text_blocks(content)
        if blocks:
            record_texts.append("\n".join(blocks))
    return "\n".join(reversed(record_texts))


# ---------------------------------------------------------------------------
# Dictionary loading — fail-open on every lifecycle problem (see module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern
    only_if_cyrillic: bool
    replacement: str
    why: str


@dataclass(frozen=True)
class Hit:
    rule_id: str
    matched: str
    replacement: str
    why: str


def _map_flags(flags: str, rule_id: str) -> int:
    """DICT.json's `flags` are authored for JS regex conventions:
      - "i" -> IGNORECASE (same meaning in Python).
      - "u" -> a no-op in Python (str patterns are already Unicode-aware, unlike JS's
        ASCII-only `\\b`). CAUTION for anyone re-verifying this: testing a RAW JS pattern
        with a plain `\\b` against non-Latin-script text (e.g. a Cyrillic-alphabet word)
        correctly shows `false` — but that is NOT what tg-cli actually runs at match time. tg-cli's
        own `compileRule` unconditionally rewrites every `\\b` into a Unicode-aware
        lookaround (`toUnicodeBoundaryPattern`, features/cli/dict-gate.ts) before
        compiling; empirically re-running the SAME pattern through that real rewrite
        returns `true`, matching Python's native behavior exactly. A reviewer comparing
        "raw JS `\\b`" against "Python's Unicode-aware `\\b`" and calling that a
        cross-runtime divergence is comparing the wrong two things — the correct
        comparison is "JS's own already-rewritten runtime pattern" vs. Python, and those
        agree (verified 2026-09-05/06, agent-tools#548's review round 2).
      - "g" -> a no-op: JS's global flag makes `matchAll` return every match, which is
        already `re.finditer`'s only behavior in Python — there is nothing to enable.
      - "m" -> MULTILINE, "s" -> DOTALL: same meaning as JS's identical flag letters.
    Every other character is an unknown flag: raised as an error so the caller can skip
    and warn about this ONE rule rather than silently ignoring a flag that might change
    matching behavior. Rejecting an actually-meaningful flag (rather than mapping it) would
    silently drop a whole rule the moment a future DICT.json edit uses it — "g" in
    particular is idiomatic JS and plausible to show up even though today's real file never
    uses it — so every JS flag DICT.json's own schema allows must map to *something*, even
    a documented no-op, rather than erroring."""
    result = 0
    for ch in flags or "":
        if ch == "i":
            result |= re.IGNORECASE
        elif ch == "m":
            result |= re.MULTILINE
        elif ch == "s":
            result |= re.DOTALL
        elif ch in ("u", "g"):
            continue
        else:
            raise ValueError(f"rule {rule_id!r} has unknown flag {ch!r}")
    return result


_NO_WHY_GIVEN = "(no reason recorded for this rule)"


def _compile_rule(raw: object, index: int, path: Path) -> tuple[Rule | None, str | None]:
    """`id`/`pattern`/`replacement` are required (matching tg-cli's own `compileRule` —
    see tg-cli's features/cli/dict-gate.ts). `why` is NOT required there, even though
    every rule in today's real DICT.json happens to carry one and this hook's block
    message wants to show it: a valid, tg-enforced rule missing `why` must still be
    compiled and enforced here, just with a fallback string, rather than silently dropped
    — a review finding (parity gap with tg-cli's schema)."""
    if not isinstance(raw, dict):
        return None, f"dictionary {path} rule #{index + 1} is not an object"
    rule_id = raw.get("id")
    label = f'"{rule_id}"' if isinstance(rule_id, str) and rule_id else f"#{index + 1}"
    for key in ("id", "pattern", "replacement"):
        if not isinstance(raw.get(key), str) or (key in ("id", "pattern") and not raw[key]):
            return None, f"dictionary {path} rule {label}: missing or non-string {key!r}"
    if "why" in raw and not isinstance(raw["why"], str):
        return None, f"dictionary {path} rule {label}: \"why\" must be a string when present"
    if "only_if_cyrillic" in raw and not isinstance(raw["only_if_cyrillic"], bool):
        return None, f"dictionary {path} rule {label}: \"only_if_cyrillic\" must be a boolean"
    if "flags" in raw and not isinstance(raw["flags"], str):
        # A review finding: without this check, a non-string, non-iterable `flags` (e.g.
        # `5`, `true`) makes `_map_flags`'s `for ch in flags or ""` raise an UNCAUGHT
        # TypeError — this except tuple only catches `re.error`/`ValueError`, so the
        # exception would propagate out of `_compile_rule` -> `load_rules()` -> `main()`
        # (which has no enclosing try there), exiting the process with a traceback instead
        # of protocol JSON. `on_error: open` still saves the turn at the descriptor level,
        # but it would silently violate THIS function's own contract ("one broken rule
        # doesn't take down the dictionary") by taking down every rule that hadn't been
        # compiled yet.
        return None, f"dictionary {path} rule {label}: \"flags\" must be a string when present"
    try:
        flags = _map_flags(raw.get("flags", ""), raw["id"])
        regex = re.compile(raw["pattern"], flags)
    except (re.error, ValueError) as exc:
        return None, f"dictionary {path} rule {label}: invalid regex ({exc})"
    rule = Rule(
        id=raw["id"],
        regex=regex,
        only_if_cyrillic=bool(raw.get("only_if_cyrillic", False)),
        replacement=raw["replacement"],
        why=raw.get("why") or _NO_WHY_GIVEN,
    )
    return rule, None


def _resolve_dict_path() -> tuple[Path, bool]:
    """(path, explicit) — explicit means CHAT_DICT_GATE_DICT_PATH named it, which changes
    how a MISSING file is treated (see module docstring / DICT_PATH_OVERRIDE comment)."""
    if DICT_PATH_OVERRIDE:
        return Path(os.path.expanduser(DICT_PATH_OVERRIDE)), True
    return Path(os.path.expanduser(DEFAULT_DICT_PATH)), False


def _load_dictionary_from_path(path: Path, *, explicit: bool) -> tuple[list[Rule], str | None]:
    """(rules, warning). `warning` is a single stderr-ready message, or None. A missing
    DEFAULT path is silently disabled (rules=[], warning=None) — a machine without Alex's
    personal dictionary is not an error. Every other failure (missing EXPLICIT path,
    unreadable file, malformed JSON/shape, or an individual rule's regex not compiling)
    fails OPEN with a warning: the gate keeps whatever rules DID compile (possibly none)
    rather than losing the whole dictionary over one bad line."""
    if not path.exists():
        if explicit:
            return [], f"dictionary path {path} (from CHAT_DICT_GATE_DICT_PATH) does not exist"
        return [], None
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"could not read dictionary {path}: {exc}"
    # A hand-edited file may carry a UTF-8 BOM (Notepad, some macOS tools); json.loads
    # rejects a leading U+FEFF. tg-cli strips it before parsing (see its loadDictionary) —
    # matching that here means a file Telegram sending already accepts isn't silently
    # disabled on this surface by one invisible byte (a review finding).
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [], f"dictionary {path} is not valid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return [], f"dictionary {path} is malformed: expected an object with a \"rules\" array"
    version = data.get("version")
    if version is not None and version != SUPPORTED_DICTIONARY_VERSION:
        # tg-cli refuses an unsupported version outright (its own SUPPORTED_DICTIONARY_
        # VERSION check) rather than guessing at a newer/older rule shape. Mirrored here
        # as a fail-OPEN warning (not a refusal — see this hook's whole on_error=open
        # posture) rather than silently parsing a "rules" array whose semantics may have
        # changed underneath this hook (a review finding).
        return [], f"dictionary {path} has version {version!r}, but this hook only understands version {SUPPORTED_DICTIONARY_VERSION}"
    rules: list[Rule] = []
    warnings: list[str] = []
    for i, raw_rule in enumerate(data["rules"]):
        rule, rule_warning = _compile_rule(raw_rule, i, path)
        if rule is not None:
            rules.append(rule)
        if rule_warning:
            warnings.append(rule_warning)
    return rules, ("; ".join(warnings) if warnings else None)


def load_rules() -> tuple[list[Rule], str | None]:
    path, explicit = _resolve_dict_path()
    return _load_dictionary_from_path(path, explicit=explicit)


# ---------------------------------------------------------------------------
# Matching + the block message
# ---------------------------------------------------------------------------


def find_hits(rules: list[Rule], text: str) -> list[Hit]:
    if not text:
        return []
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        if rule.only_if_cyrillic and not has_cyrillic:
            continue
        for m in rule.regex.finditer(text):
            matched = m.group(0)
            if not matched:
                continue
            key = (rule.id, matched)
            if key in seen:
                continue
            seen.add(key)
            hits.append(Hit(rule_id=rule.id, matched=matched, replacement=rule.replacement, why=rule.why))
    return hits


def _format_block_message(hits: list[Hit]) -> str:
    lines = [
        f'  [{h.rule_id}] matched "{h.matched}" — {h.why} Use instead: {h.replacement}'
        for h in hits
    ]
    return (
        "Your reply just used wording that is on Alex's banned-word list and the turn "
        f"cannot end as written. {len(hits)} violation(s):\n" + "\n".join(lines) +
        "\n\nRewrite your reply without this wording. When you explain what you changed, "
        "do NOT repeat or reference the banned word itself (e.g. do not write something "
        "like \"changing X to Y\" naming the banned word as X) — doing so re-triggers this "
        "exact check. Just use the correct wording directly."
    )


# ---------------------------------------------------------------------------
# Decision + main
# ---------------------------------------------------------------------------


def _next_decision(prior_count: int, hits: list[Hit]) -> tuple[str, str | None, int, int]:
    """Pure policy — no I/O. Returns (log_decision, message, exit_code,
    counter_to_persist). `log_decision` is the value written to firings.jsonl;
    `_apply_decision` below derives what actually goes on STDOUT, and may override this
    function's `log_decision`/`message`/`exit_code` if the counter write fails (see
    there)."""
    if not hits:
        return "allow", None, 0, 0
    new_count = prior_count + 1
    if new_count > LOOP_GUARD_CAP:
        return "allow_loop_guard_cap", None, 0, 0
    return "block", _format_block_message(hits), BLOCK_EXIT_CODE, new_count


def _apply_decision(sid: str, hits: list[Hit], start: float) -> tuple[str, str | None, int]:
    """Runs the pure `_next_decision`, persists the loop-guard counter + firings log, and
    returns what `main()` should actually emit on stdout: (decision, message, exit_code).

    Two write-failure/bookkeeping cases are NOT simply "run the pure decision, write,
    done":
      - If the pure decision is "block" but the counter write FAILS, this Stop's violation
        can never be recorded — a future Stop would read the counter as if THIS one never
        happened, so the streak could never reach `LOOP_GUARD_CAP` and the guard would be
        silently dead (a real review finding: a read-only/full `MARKER_DIR` would turn this
        hook's fail-OPEN contract into fail-CLOSED-forever, the exact outcome it exists to
        prevent). So a block whose counter write fails is escalated to an allow instead —
        this hook's on_error=open posture applies to persistence failures too, not only to
        the dictionary lifecycle.
      - The `allow_loop_guard_cap` decision is internal bookkeeping (it exists so
        `firings.jsonl` can distinguish "gave up" from an ordinary clean-turn allow). On
        the wire it is collapsed to a plain `"allow"` — nothing in the agents-hooks/v1
        contract needs a third decision value, and a stricter future runner validating
        `decision` against `{allow, block}` must not choke on it (a review finding).
    """
    prior = _read_counter(sid)
    log_decision, message, exit_code, counter_to_persist = _next_decision(prior, hits)
    rule_ids = [h.rule_id for h in hits]

    wrote = _write_counter(sid, counter_to_persist)
    if log_decision == "block" and not wrote:
        # A DIFFERENT firings-log label than "allow_loop_guard_cap" (a review finding):
        # "the model is stuck in a genuine rewrite loop" and "the marker dir became
        # unwritable" need different remediation (nudge the model vs. fix disk/perms) —
        # collapsing them into one label would defeat the whole point of logging a
        # distinct decision for the loop-guard case in the first place.
        warn(
            f"loop-guard counter for session {sid} could not be persisted — allowing this "
            f"stop despite {len(hits)} unresolved dictionary hit(s) rather than risk an "
            "un-cappable block loop"
        )
        log_decision, message, exit_code = "allow_counter_write_failed", None, 0
    elif log_decision == "allow_loop_guard_cap":
        warn(
            f"loop guard cap ({LOOP_GUARD_CAP}) exceeded for session {sid} — allowing the "
            f"stop despite {len(hits)} unresolved dictionary hit(s) to avoid wedging the "
            "session; a config or model issue may be preventing a clean rewrite"
        )

    _log_firing(sid, log_decision, rule_ids, _elapsed_ms(start))
    # Both bookkeeping labels are internal to firings.jsonl — the agents-hooks/v1 contract
    # only needs to know the stop was allowed, not why.
    stdout_decision = "allow" if log_decision != "block" else "block"
    return stdout_decision, message, exit_code


def main() -> int:
    start = time.monotonic()
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing stop (fail-open)")
        emit("allow")
        return 0
    if not isinstance(event, dict):
        event = {}

    if _disabled():
        emit("allow")
        return 0

    rules, dict_warning = load_rules()
    if dict_warning:
        warn(dict_warning)
    if not rules:
        # No dictionary configured (or every rule failed to load) -> this Stop can never
        # produce a hit, so persisting a loop-guard marker or a firings.jsonl row for it
        # would just be unbounded, pointless disk growth on every machine that isn't even
        # using this feature (a review finding — the gate being "silently disabled" must
        # mean no accumulating state, the same posture the kill switch already has, not
        # only "no stderr noise"). BUT: if a PRIOR streak already wrote a counter for this
        # session (a real violation happened before the dictionary transiently broke —
        # e.g. mid-edit on DICT.json), it must still be reset here, or a later real
        # violation would resume from a stale prior count and trip the cap early — a
        # review finding that the "clean turn always resets to 0" guarantee must hold even
        # on this early-return path, not only on the ordinary zero-hits path below.
        sid = session_id(event)
        if _counter_file(sid).exists():
            _write_counter(sid, 0)
        emit("allow")
        return 0

    text = _extract_current_turn_text(_transcript_path(event))
    hits = find_hits(rules, text)

    sid = session_id(event)
    decision, message, exit_code = _apply_decision(sid, hits, start)
    emit(decision, message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
