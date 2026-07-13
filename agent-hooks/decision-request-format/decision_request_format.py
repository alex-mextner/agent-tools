#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — advisory self-check on an escalation to the human.

When the agent escalates to the human's out-of-band channel — a ``tg --tag decision`` /
``--tag problem`` / ``--tag question`` invocation — this inspects the message body for the
structural markers the `decision-request-discipline` skill's mandatory self-check requires
(Context, Options / pros-cons table, Recommendation). All three tags are the same escalation
shape, so the self-check applies to each — an agent picking ``problem`` or ``question`` instead
of ``decision`` no longer skips it silently (agent-tools#213/#12). If any marker is missing, it
emits an **exit-0 ADVISORY** naming what's absent, so the agent can rewrite before the human gets
a bare "A or B?".

It is deliberately **advisory, never a block.** A malformed decision request must stay
sendable — the human would rather receive an imperfect escalation than have the send wedged
by a heuristic. So this NEVER returns exit 10; it reminds (allow + message) and lets the
send proceed. The deterministic value it adds over the skill alone: it fires at *send time*,
the exact moment the self-check is supposed to run but gets skipped under load — the one
moment a skill can't.

Matching is by proper parsing, not raw-string search: the command is split into ``&&``/
``;``/``|`` segments, leading no-op wrappers (``env``, ``timeout``, ``nice``, …) are peeled,
and each segment is shlex-tokenized so a ``tg`` substring in a path or another flag's value
never trips it — only an actual ``tg`` command carrying ``--tag decision`` does.

NOT subagent-exempt: the self-check binds every agent, including the subagent that (per the
skill) drafts the request. A subagent posting a malformed decision request is exactly what
this catches.

External silence (replaces the OLD self-service escape hatch): there is NO
``ALLOW_RAW_DECISION_REQUEST`` env or ``# decision-request-ok:`` inline sentinel any more — an
agent could set either on its own command, so those merely silenced the nag by self-grant. A
one-time silence is now requested by setting
``RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<written justification>"``, which routes a single
approval request to the human via Telegram (deny-by-default; a bare ``1`` is rejected). On an
approved request the advisory is silenced; otherwise it is shown. This never changes the
exit-0/allow contract — it only decides whether the advisory prints.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow (always)   exit 10 : BLOCK (never used)

on_error is "open": a crash here must never wedge the ability to send a message — this is a
formatting reminder, not a boundary.
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

HOOK_API = "agents-hooks/v1"

# Cheap pre-filter: a `tg` token appears at all (word-boundary). It deliberately does NOT
# anchor at a command start — a wrapped `timeout 30 tg …` has `tg` mid-line, and the
# authoritative check is the per-segment shlex unwrap + `tg`-head match below, not this. This
# only decides whether tokenizing is worth it; a path like `/opt/tg/bin` that survives here is
# rejected by the argv-head fullmatch.
_TG_CMD = re.compile(r"\btg\b")

# Leading no-op wrappers that prefix the real command without changing it (`env TG_AI_MODEL=
# claude tg …`, `timeout 30 tg …`). The real runner sits after the wrapper + its args, so we
# peel them before tokenizing. (Same conservative set as no-long-inline-process.)
_WRAPPERS = re.compile(r"^(?:timeout|env|nice|time|stdbuf|nohup|setsid|unbuffer)$")

# A bare leading `NAME=VALUE` assignment prefix (`TG_AI_MODEL=claude tg …`) — NO `env` keyword.
# This is the DOMINANT form: the decision-request-discipline skill literally tells the agent to
# run `TG_AI_MODEL=claude tg --tag decision …`, so without peeling this the gate would miss its
# own prescribed call. Mirrors the same peel in no-shell-file-edit. (`env NAME=VALUE …` is
# handled by the wrapper loop; this is the keyword-less shell assignment.)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Structural markers the self-check requires in a decision-request body. Each maps a
# human-facing label to the regex that detects its presence (case-insensitive). Kept
# deliberately permissive — the goal is to catch a body that mentions NONE of a dimension,
# not to grade phrasing.
_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `\S+\.\w+:\d+` matches the `file.ext:line` form the skill's format point 1 prescribes
    # (`app.py:42`, `src/loader.ts:128`), so a body that gives a code ref instead of the literal
    # word "context" is not falsely flagged as missing Context. The `.ext` requirement keeps a
    # bare `host:port` / `12:30` from being mistaken for a file ref and silencing the advisory.
    ("Context", re.compile(r"\bcontext\b|\bbackground\b|file:|\S+\.\w+:\d+|where the code", re.I)),
    ("Options", re.compile(r"\boptions?\b|\bvariants?\b|\bpros?\b|\bcons?\b|\btrade-?offs?\b", re.I)),
    ("Recommendation", re.compile(r"\brecommend|\bi suggest\b|\bi'd (?:go|pick)\b|\bproposal\b", re.I)),
)

# The escalation-shaped `tg` tags this hook inspects. All three route a message to the human's
# out-of-band channel expecting a structured escalation (context + pros/cons + a recommendation),
# per the decision-request-discipline skill — so the same self-check applies to each. `decision`
# was the original tag; `problem` and `question` are the same escalation shape and were added so
# the self-check is not silently skipped just because the agent picked a different tag word.
# (agent-tools#213/#12.)
_ESCALATION_TAGS = frozenset({"decision", "problem", "question"})


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"decision-request-format: {msg}\n")


def _allow(message: str | None = None) -> int:
    emit("allow", message)
    return 0


def _unwrap_segment(segment: str) -> list[str]:
    """Tokenize one command segment and strip leading no-op wrappers, returning the wrapped
    command's tokens (`env TG_AI_MODEL=claude tg "x"` → `['tg', 'x']`). A non-wrapper segment
    is returned tokenized as-is. Returns ``[]`` if the segment can't be tokenized."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return []
    # Peel a bare `NAME=VALUE` assignment prefix (`TG_AI_MODEL=claude tg …`) — possibly several
    # (`A=1 B=2 tg …`) — so the real command head is exposed. Done before the wrapper loop so
    # `A=1 timeout 30 tg …` also unwraps fully.
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    while tokens and _WRAPPERS.match(tokens[0]):
        wrapper, rest = tokens[0], tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in ("-s", "-k", "--signal", "--kill-after", "-n", "-u"):
                    i += 1
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional
            break
        if i:
            tokens = rest[i:]
        elif rest:
            tokens = rest
        else:
            break
    return tokens


def _escalation_request_body(command: str) -> tuple[str, str] | None:
    """If the command is a `tg` invocation carrying an escalation tag (``--tag decision`` /
    ``problem`` / ``question``), return ``(tag, body)`` — the matched tag and the message body it
    would send (positional text + any ``--title``); else None.

    Walks each ``&&``/``;``/``|``-separated segment so a `tg` later in a pipeline is seen.
    Uses shlex tokenization, not raw matching, so the tag and body are read exactly as `tg`
    would receive them."""
    if not _TG_CMD.search(command):
        return None
    for raw_segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        tokens = _unwrap_segment(raw_segment)
        if not tokens or not re.fullmatch(r"(?:\S*/)?tg", tokens[0]):
            continue
        result = _body_if_escalation_tag(tokens[1:])
        if result is not None:
            return result
    return None


def _body_if_escalation_tag(args: list[str]) -> tuple[str, str] | None:
    """Given a `tg` invocation's args, return ``(tag, body)`` iff a ``--tag`` from
    ``_ESCALATION_TAGS`` (decision/problem/question) is present; else None. Collects positional
    (non-flag) text and the ``--title`` value, which together are what the human reads.
    Value-taking flags consume their next token so a flag value is never mistaken for the
    positional message."""
    matched_tag: str | None = None
    title = ""
    positionals: list[str] = []
    # The COMPLETE set of value-taking `tg` flags other than --tag/--title (handled above), so
    # a flag's VALUE is never collected as body text. If a future `tg` adds a value flag not
    # listed here, its value would fall to the `tok.startswith("-")` toggle branch and its
    # argument would leak into `positionals` — a false NEGATIVE only (a leaked value could
    # accidentally satisfy a marker → no advisory), never a false positive, and this hook is
    # advisory regardless. Keep this in sync with `tg --help`'s `--flag <value>` entries.
    value_flags = {"--photo", "--file", "--reply-to", "--pdf-device", "--format"}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--tag":
            if i + 1 < len(args) and args[i + 1].lower() in _ESCALATION_TAGS:
                matched_tag = args[i + 1].lower()
            i += 2
            continue
        if tok.startswith("--tag="):
            candidate = tok.split("=", 1)[1].lower()
            if candidate in _ESCALATION_TAGS:
                matched_tag = candidate
            i += 1
            continue
        if tok == "--title":
            if i + 1 < len(args):
                title = args[i + 1]
            i += 2
            continue
        if tok.startswith("--title="):
            title = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in value_flags:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            i += 1  # a bare/unknown flag (toggle) — no value to skip
            continue
        positionals.append(tok)
        i += 1
    if matched_tag is None:
        return None
    return matched_tag, "\n".join([title, *positionals]).strip()


def _missing_markers(body: str) -> list[str]:
    return [label for label, pat in _MARKERS if not pat.search(body)]


def _advisory_message(tag: str, missing: list[str]) -> str:
    # `missing` is only ever a subset of the three markers this hook can detect heuristically
    # (Context / Options / Recommendation). The skill asks for two more — a glossary of terms
    # and a "where to look" code ref — that a regex can't reliably check; they're named as a
    # separate reminder, NOT folded into the `missing` list, so the list stays honest about
    # what was actually measured.
    return (
        "Escalation self-check (advisory — your message still sends): this "
        f"`tg --tag {tag}` body appears to be missing {', '.join(missing)} (of the three "
        "markers this hook checks). Read the decision-request-discipline skill and send a "
        "structured escalation: context (a 'where to look' code ref + a glossary of internal "
        "terms), a pros/cons table of the options, and a recommendation — so the human can "
        "decide in 30s without opening the repo. Consider rewriting before sending — or, for a "
        'genuine one-time silence, request approval via '
        'RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<why>" (Telegram approval, deny-by-default).'
    )


def _advisory_for(command: str, cwd: str) -> str | None:
    """The advisory message for a command, or None when nothing should be said (not a decision
    request, externally silenced via an approved hatch, or a complete body).

    When an advisory WOULD be shown (a decision-request body missing markers), an approved
    Telegram hatch (`RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT`) silences it; an unset / invalid /
    denied request leaves the advisory in place. The hatch call never raises (it catches
    internally), so this stays fail-open."""
    matched = _escalation_request_body(command)
    if matched is None:
        return None
    tag, body = matched
    missing = _missing_markers(body)
    if not missing:
        return None
    hatch = hatch_escalation.request_hatch_approval(
        "decision-request-format",
        {"hook": "decision-request-format", "command": command},
        cwd=cwd,
        command=command,
    )
    if hatch.should_stop and hatch.approved:
        warn(f"advisory silenced via hatch escalation ({hatch.reason})")
        return None
    return _advisory_message(tag, missing)


def main() -> int:
    # The whole body is fail-open: ANY unexpected error (a malformed event, an input shlex
    # can't tokenize despite the guards) returns `allow`, never a non-zero exit. This is an
    # advisory formatting nudge, not a boundary — it must never wedge the ability to send a
    # message. So every path through `main` returns 0/allow.
    try:
        event = json.load(sys.stdin)
        args = event.get("args") or {}
        command = args.get("command") or args.get("cmd") or event.get("command") or ""
        if not isinstance(command, str):
            command = str(command)
        cwd = str(event.get("cwd") or os.getcwd())
        return _allow(_advisory_for(command, cwd))
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all: fail-open is the contract
        warn(f"could not inspect command: {exc} — allowing (fail-open)")
        return _allow()


if __name__ == "__main__":
    sys.exit(main())
