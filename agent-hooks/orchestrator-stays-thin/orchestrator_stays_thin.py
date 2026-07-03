#!/usr/bin/env python3
"""agents-hooks/v1 pre-write + pre-bash hook — keep the orchestrator thin.

The orchestrator plans, dispatches, and verifies — it does NOT implement inline. When the
MAIN thread is about to do implementation-shaped work itself (a non-docs code Edit/Write, or
a multi-step implementation Bash) this gate nudges it to delegate to a subagent or a
Workflow. It enforces `delegate-work-to-subagents`.

ONE script binds TWO points via two descriptors; it branches on ``event["point"]``:
  - pre-write : a CODE Edit/Write (non-docs) by the main thread → warn-then-block
  - pre-bash  : a clearly multi-step / implementation-shaped Bash by the main thread →
                warn-then-block. Read-only inspection is NEVER blocked — a single one-liner
                (git status, ls, cat, grep, find) OR a fully read-only chain of any length
                across |/&&/;/||/newline (find ... | grep ... | head, git status && ls).
                A `gh ship` RELEASE chain is NEVER blocked either — the gated merge is the
                orchestrator's own action (#159, see _is_sanctioned_release_chain).

TIERED (warn → block): the FIRST offense in the TTL window WARNs (allow + message); a REPEAT
in the window BLOCKs. The tier is tracked by a marker file keyed by a hash of cwd. This gives
the doctrine's "WARN then BLOCK" instead of a hard wall on the first inline edit.

Subagent-exempt: a dispatched subagent (``agent_id`` present) does the actual work, so it is
always allowed — this gate governs the orchestrator only.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_ORCHESTRATOR_WORK=1            — disable the guard for this session (both points)
  - env  ALLOW_ORCHESTRATOR_WORK_REASON=...   — REQUIRED with the override; logged
  - inline (PRE-BASH ONLY)  `# orchestrator-ok: <reason>`  — self-documenting per-command.
    A pre-write carries no shell string, so the inline sentinel can only fire for a bash
    command; for a write use the ENV hatch.
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command (bash) or args.file_path/path (write); event.point
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orchestrator-thin")))
# How long a first-offense WARN suppresses the next WARN before a REPEAT becomes a BLOCK.
TTL_S = int(os.environ.get("ORCH_THIN_TTL_S", "900"))

# A write to one of these is documentation, never implementation → always allow.
DOCS_PATH = re.compile(r"\.(?:md|mdx|txt|rst)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)

# Inspection / read-only one-liners that the orchestrator legitimately runs itself. The system-info
# and text-filter tools (df/du/lsblk/free/ps/uname/…, jq/sort/uniq/cut/column) are read-only
# VERIFICATION the orchestrator does directly — added so a multi-step verify pipe (`df -h | grep
# /dev | head`, `gh pr view | jq | head`) is not blocked by the >=2-operator rule (coordinator).
READ_ONLY_BASH = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|less\b|head\b|tail\b|"
    r"grep\b|rg\b|find\b|pwd\b|echo\b|which\b|env\b|wc\b|stat\b|tree\b|file\b|"
    r"jq\b|sort\b|uniq\b|cut\b|column\b|df\b|du\b|lsblk\b|free\b|ps\b|uname\b|"
    r"uptime\b|whoami\b|hostname\b|nproc\b|date\b)"
)
# Any chain operator (&&, ||, ;, |, newline). A command that chains AT ALL is not a single
# read-only invocation, so the read-only carve-out below must NOT short-circuit it (B1).
CHAIN = re.compile(r"&&|\|\||;|\||\n")
# A command starts at the line start or right after a &&/;/|/( separator. Anchoring the
# build-tool tokens here means the runner must be the COMMAND, not an argument/needle —
# the same anchoring the no-long-inline-process sibling uses (#5).
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"
# A heredoc, or an obvious build/edit invocation, marks implementation-shaped shell.
HEREDOC = re.compile(r"<<-?\s*['\"]?\w+")
# In-place edit / build invocations. A bare `>`/`>>` redirect is NOT here: a redirect alone
# ("python foo.py > out.log") is not implementation — the in-place editors (sed -i, tee) and
# the build tools are the real signals (B7). The build-tool RUNNERS are anchored at a command
# head so a substring needle in an inspection pipe (`cat notes.md | grep npm`, `git log | rg
# yarn`, `find . -name cargo.toml | wc -l`) is NOT mis-read as implementation (#5). The
# in-place editors (sed -i, tee) keep a bare `\b` anchor: they are a content signal wherever
# they appear (e.g. `git status && sed -i ...`).
BUILD_EDIT = re.compile(
    r"\b(?:sed\s+-i|tee)\b"
    r"|" + _CMD_START + r"(?:npm|pnpm|yarn|bun|cargo|go\s+build|make|"
    r"python\s+setup|pip\s+install)\b"
)
INLINE_SENTINEL = re.compile(r"#\s*orchestrator-ok:\s*(\S.*)")

# Sanctioned read-only ORCHESTRATION verbs the orchestrator runs ITSELF to VERIFY and REPORT — never
# a mutation, so a chain of only these (+ read-only inspection / cd) is verify-and-report altitude,
# not implementation. The orchestrator's role is literally "verify + report", so reporting (`tg`)
# and PR/CI verification (gh reads) must not require a subagent (coordinator directive). Head-anchored
# like READ_ONLY_BASH. Deliberately NOT `curl` or `ssh`: neither can be reliably classified read-only
# (`curl -X POST`/`-d …` mutates; `ssh host '…'` runs ANY remote command) — those keep the escape hatch.
ORCH_READONLY = re.compile(
    r"^\s*(?:tg\b"
    r"|gh\s+pr\s+(?:list|view|checks|status|diff)\b"
    r"|gh\s+run\s+(?:list|view)\b)"
)

# `gh ship` — the sanctioned gated-merge (RELEASE) command — at a segment HEAD, optionally
# after env-var assignment prefixes (`GH_PAGER=cat gh ship 605`). Anchored on the segment's
# argv like the other head tokens, NEVER matched as a substring in text: a needle such as
# `grep 'gh ship' log` must not self-exempt a chain (#159).
# The env-prefix value is a single unambiguous `\S*` (a run of non-space) — NOT an alternation with
# quoted forms. An earlier `(?:'…'|"…"|\S*)` overlapped `\S*` with the quoted branches and caused
# catastrophic backtracking on `A="" A="" …` (CodeQL py/redos, HIGH). `\S*` and the trailing `\s+`
# are disjoint char classes, so the repeat is linear. Cost: an env value containing a SPACE
# (`X="a b" gh ship`, rare on a ship line) no longer gets the fast path — it falls to ordinary
# judgement. Realistic prefixes (`GH_PAGER=cat`, `GH_TOKEN=x`, `X="cat"`) still match.
GH_SHIP_HEAD = re.compile(
    r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*gh\s+ship(?=\s|$)"
)
# The only extra companion a release line may carry besides read-only inspection: `cd`
# changes no repo state, and `cd <repo> && gh ship <PR> | tail` is a common ship shape.
# Anchored on an argv boundary (`\s`/end), NOT `\b`: a bare `\b` matches before punctuation, so
# `cd-clean` / `cd/foo` (other commands) would ride the carve-out — they must not (#159 review P1).
CD_HEAD = re.compile(r"^\s*cd(?=\s|$)")
# A command or process substitution can smuggle ANY mutation into an otherwise-benign
# release segment (`cd $(git push ...) && gh ship`, `cat <(git push ...) && gh ship`), and
# BUILD_EDIT only knows build/edit tools — so the release carve-out vetoes substitutions
# wholesale, like heredocs: `$(...)`, backticks, and process substitution `<(...)`/`>(...)`.
# A legit ship line does not need them; a rare `cd $(git rev-parse ...) && gh ship` just
# falls back to the ordinary judgement (#159).
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
# A single `&` (backgrounding / control operator) is NOT a CHAIN split, so `gh ship 605 &
# git push` would read as ONE segment with a ship head — the release carve-out vetoes it.
# Redirect `&`s (`2>&1`, `&>`, `>&`) and the already-split `&&` are excluded (#159).
BG_AMP = re.compile(r"(?<![&>])&(?![&>])")
# READ_ONLY_BASH has a documented inherited gap (#80): a read-only HEAD can still carry a
# mutating form — `find . -delete`/`-exec ...`, the `env <cmd>` wrapper, `git branch -D`.
# The plain read-only carve-out tolerates that gap (out of scope there), but the release
# carve-out must not EXTEND it to ship lines that were blocked pre-#159 — so these forms
# are excluded from the ship-companion set (#159). Head-anchored for env / git branch;
# find's mutating actions are a content signal anywhere in the segment. This covers BOTH find's
# delete/exec family AND its file-WRITING primaries (`-fprint`/`-fprintf`/`-fprint0`/`-fls`),
# which write an arbitrary path just like `-delete` mutates — omitting them let
# `gh ship | find . -fprintf evil.sh 'x' | tail` ride the carve-out (#159 review, main finding).
# SYNC with READ_ONLY_BASH: of its git subcommands (status|log|diff|show|branch), only
# `branch` has a mutating argument form — the rest are read-only whatever their args. If a
# subcommand with a mutating form (tag/config/stash/notes/…) is ever added there, it must
# be excluded here too.
UNSAFE_COMPANION = re.compile(
    r"^\s*(?:env\b|git\s+branch\s+\S)"
    r"|\s-(?:delete|exec|execdir|ok|okdir|fprintf|fprint0|fprint|fls)\b"
)

MESSAGE = (
    "Delegate to a subagent: the orchestrator plans, dispatches, and verifies — it does not "
    "implement inline. Launch an Agent (run_in_background: true) or a Workflow to do this "
    "Edit/Write/Bash. (delegate-work-to-subagents, enforced.)"
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"orchestrator-stays-thin: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    TRUST BOUNDARY — this gate uses agent_id to RELAX (a subagent is exempt from the
    thin-orchestrator block), so the read surface must be exactly the one the bridge sanitizes,
    and no wider. We read ONLY `args.agent_id`: lib/cc_hook_bridge enforces T2 precedence before
    the event reaches us — it OVERWRITES args.agent_id from CC's authoritative top-level field
    when CC supplies it and DROPS any model/tool_input-supplied copy when CC does not. So the only
    way a truthy `args.agent_id` survives is if CC itself dispatched a subagent; a main-thread
    agent cannot forge it.

    We deliberately DO NOT fall back to a top-level `event.get("agent_id")`: the bridge NEVER
    writes a top-level `agent_id` (its v1 event has no such key), so that fallback was a DEAD path
    that was nonetheless a TRUSTED, unsanitized relax-surface — if any non-bridge producer ever set
    a top-level agent_id from a model-influenced field, the orchestrator would self-exempt. This
    narrows the read to the sanitized `args.agent_id` only, matching the sibling skills-read-gate
    (agent-tools#115, bringing the two gates into line per #112/#116). An empty/whitespace value is
    not a subagent."""
    args = event.get("args") or {}
    aid = args.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str, point: str) -> str | None:
    """The override reason if a valid escape hatch is present, else None.

    Honored ONLY with a reason:
      - env ALLOW_ORCHESTRATOR_WORK=1 + ALLOW_ORCHESTRATOR_WORK_REASON (both points), OR
      - an inline ``# orchestrator-ok: <reason>`` — PRE-BASH ONLY. A pre-write carries no shell
        string, so the inline sentinel genuinely cannot fire for a write; only the ENV hatch
        applies there (B4). A reasonless override is ignored.
    """
    if os.environ.get("ALLOW_ORCHESTRATOR_WORK") == "1":
        reason = (os.environ.get("ALLOW_ORCHESTRATOR_WORK_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    if point == "pre-bash":
        m = INLINE_SENTINEL.search(command)
        if m:
            return f"inline override: {m.group(1).strip()}"
    return None


def _marker(event: dict) -> Path:
    # Key by (cwd, point) so a pre-write WARN does not prime a pre-bash BLOCK (or vice versa):
    # each point tiers independently (B5).
    cwd = str(event.get("cwd") or "default")
    point = str(event.get("point") or "")
    sid = hashlib.sha256(f"{cwd}\0{point}".encode()).hexdigest()[:16]
    return MARKER_DIR / f"{sid}.warned"


def _is_repeat(event: dict) -> bool:
    """True if a WARN already fired in this cwd within the TTL window (→ now BLOCK)."""
    m = _marker(event)
    try:
        if m.exists() and (time.time() - m.stat().st_mtime) <= TTL_S:
            return True
    except OSError:
        return False
    # First offense → record the warn marker so the next one in the window blocks.
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        m.write_text(str(time.time()))
    except OSError as exc:
        warn(f"could not write warn marker {m}: {exc} — staying in WARN tier")
    return False


def _is_code_write(args: dict) -> bool:
    # NotebookEdit carries `notebook_path`; the bridge also aliases it onto file_path/path, but
    # read it directly too so a `.ipynb` edit is judged even if only notebook_path is set (B3).
    path = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
    if not isinstance(path, str) or not path.strip():
        return False  # no path to judge → don't claim it's a code write
    if DOCS_PATH.search(path) or DOCS_DIR.search(path):
        return False  # docs are not implementation
    return True


def _is_all_read_only(command: str) -> bool:
    """True when EVERY chain segment's HEAD is a read-only inspection command.

    A fully read-only pipe (`find ... | grep ... | head`, `tail X | grep Y | wc -l`) is the
    orchestrator's bread-and-butter inspection and must never be blocked, no matter how many
    segments it has (#80).

    The judgement is per-segment-HEAD, NOT a whole-string scan: a build/edit token that appears
    only as an ARGUMENT/needle of a read-only command (`cat tee.log`, `grep cargo notes.txt`)
    must stay allowed — exactly the single-command carve-out this replaced (`tee`/`sed -i` are
    unanchored in BUILD_EDIT, so a whole-string scan would mis-flag them). A real build/edit or
    heredoc segment has a NON-read-only head (`sed ...`, `tee ...`, `npm ...`, the heredoc body),
    so it breaks `all(...)` and the caller then judges the command as implementation. HEREDOC is
    additionally vetoed up front — no inspection one-liner contains a `<<WORD` redirect.
    """
    if HEREDOC.search(command):
        return False
    # NOTE: judged on the segment HEAD only — READ_ONLY_BASH is head-anchored (`^\s*…`), so a
    # build/edit head with a read-only word in its args (`sed -i … grep.py`) is NOT waved through
    # (regression-guarded by test_build_edit_head_with_read_only_needle_blocks). Inherited gap
    # (pre-#80, also true of the old single-command carve-out): a read-only HEAD with a mutating
    # flag — `find . -delete`, `find . -exec sed -i …` — reads as inspection. Out of scope here.
    segments = [s for s in CHAIN.split(command) if s.strip()]
    return bool(segments) and all(READ_ONLY_BASH.search(s) for s in segments)


def _blank_quoted(command: str) -> str:
    """Replace the CONTENT of '…'/"…" spans with spaces (keeping the quote chars) so a whole-string
    veto scan (SUBSTITUTION / BG_AMP / BUILD_EDIT) does not fire on a shell metachar that lives
    INSIDE a quoted `gh ship` argument — a reason like `--no-screenshot-ok 'revert; reship'` or a
    `--title "a & b"` must not forfeit the carve-out and false-block a legit ship (#159 review
    F2/P2). Best-effort (matches _split_release_segments): backslash-escapes are out of scope."""
    out: list[str] = []
    quote: str | None = None
    for c in command:
        if quote is not None:
            out.append(c if c == quote else " ")
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
            out.append(c)
        else:
            out.append(c)
    return "".join(out)


def _split_release_segments(command: str) -> list[str]:
    """Quote-aware split on the chain operators (``&&`` ``||`` ``;`` ``|`` newline) that lie
    OUTSIDE quotes, so a quoted operator in a ship reason (`gh ship 605 'fix; reship'`) is NOT a
    segment boundary (#159 review F2/P2). The plain read-only path keeps the naive CHAIN split
    (unchanged, out of scope); only the release carve-out needs quote-awareness because ship
    carries free-form reason args. SYNC with workflow-guards `_split_chain`."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
        elif command[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
        elif c in (";", "|", "\n"):
            segs.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    segs.append("".join(buf))
    return [s for s in segs if s.strip()]


def _is_sanctioned_release_chain(command: str) -> bool:
    """True when the line is a `gh ship` RELEASE invocation: at least one segment HEAD is
    `gh ship`, and EVERY segment head is `gh ship`, read-only inspection, or `cd`.

    Why a carve-out at all (#159): `gh ship` is the ONE repo mutation that belongs at
    ORCHESTRATOR altitude — the gated merge. The auto-mode classifier denies it inside
    subagents (a subagent relaying its own merge is exactly what the gates exist to stop),
    so when this hook warn-then-blocked the orchestrator's inline ship chains too, NOBODY
    could reliably run the sanctioned merge command — a deadlock, observed live as flapping
    (first ship WARNs through, the next ones in the TTL window BLOCK). Release is dispatch/
    verify-altitude work like the rest of the orchestrator's job, not implementation.

    Deliberately NARROW, judged per segment HEAD like _is_all_read_only (#80):
      - `gh ship` must be a segment's argv head (env-var prefixes allowed) — a substring
        (`grep 'gh ship' log`, `echo gh ship`) exempts nothing;
      - a ship segment does NOT launder the rest of the line — `sed -i ... && gh ship`,
        `npm run build && gh ship`, or any heredoc still counts as implementation;
      - BUILD_EDIT, command SUBSTITUTIONS, and a bare background `&` are vetoed on the WHOLE
        string, mirroring the heredoc veto: this predicate fires BEFORE the caller's
        BUILD_EDIT check, and segment heads alone would miss a mutation hidden in
        `$(...)`/backticks (`gh ship $(sed -i ...)`, `cd $(git push ...) && gh ship`) or
        behind a single `&` (`gh ship 605 & git push` — CHAIN does not split on it).
        Pre-fix, the whole-string BUILD_EDIT scan caught the build/edit cases — the vetoes
        keep that and cover mutations BUILD_EDIT does not know. Cost: a build/edit token as
        a mere needle in a ship chain (`gh ship | grep tee`, and notably `gh ship | tee
        ship.log`) forfeits the carve-out and falls back to the ordinary judgement — log a
        ship with a bare redirect instead (`gh ship > log 2>&1`, not implementation, B7).
        `|&` is likewise not a supported release shape (its `&` trips the bare-`&` veto —
        and even without it, CHAIN's `|` split would leave a `& ...` segment head that
        never qualifies) — pipe with `2>&1 |`;
      - READ_ONLY_BASH's inherited head-with-mutating-flag gap (#80) is NOT extended to
        ship lines: an `env <cmd>` wrapper, `git branch <arg>`, or a find-style mutating
        action (`-delete`/`-exec`/`-ok`) disqualifies a companion (UNSAFE_COMPANION);
      - ONLY `gh ship` rides this: no other gh subcommand, no tg, nothing else.
    """
    # Veto on a QUOTE-BLANKED copy so a metachar inside a quoted ship arg does not fire (F2/P2);
    # a real substitution / bare `&` / build-edit / heredoc OUTSIDE quotes still trips it.
    scan = _blank_quoted(command)
    if (HEREDOC.search(scan) or BUILD_EDIT.search(scan)
            or SUBSTITUTION.search(scan) or BG_AMP.search(scan)):
        return False
    segments = [s for s in _split_release_segments(command) if s.strip()]
    if not any(GH_SHIP_HEAD.match(s) for s in segments):
        return False
    return all(
        GH_SHIP_HEAD.match(s)
        or ((READ_ONLY_BASH.search(s) or CD_HEAD.match(s))
            and not UNSAFE_COMPANION.search(s))
        for s in segments
    )


def _is_report_or_verify_chain(command: str) -> bool:
    """True when EVERY segment head is report/verify orchestration (`tg`, gh PR/CI reads), read-only
    inspection, or `cd`, AND at least one is the `tg`/gh-read orchestration.

    Report and verification are the orchestrator's OWN altitude — a multi-step report/verify chain
    (`tg … | tail -3 | grep merged`, `gh pr view 5 | jq .title | head`, `tg done; gh pr view 5`)
    must never warn-then-block as implementation, exactly like the release carve-out (coordinator
    directive: reporting + verification must not require a subagent). Same vetoes and quote-aware
    split as `_is_sanctioned_release_chain`: a build/edit, heredoc, command/process substitution,
    bare-`&`, or mutating companion (`git branch <arg>`, find delete/exec/file-write) forfeits it —
    so `tg done && sed -i …` or `gh pr view 5 && git push` is still judged as implementation."""
    scan = _blank_quoted(command)
    if (HEREDOC.search(scan) or BUILD_EDIT.search(scan)
            or SUBSTITUTION.search(scan) or BG_AMP.search(scan)):
        return False
    segments = [s for s in _split_release_segments(command) if s.strip()]
    if not any(ORCH_READONLY.match(s) for s in segments):
        return False
    return all(
        (ORCH_READONLY.match(s) or READ_ONLY_BASH.search(s) or CD_HEAD.match(s))
        and not UNSAFE_COMPANION.search(s)
        for s in segments
    )


def _is_implementation_bash(command: str) -> bool:
    if not command.strip():
        return False
    # A fully read-only pipe of ANY length is inspection, never implementation (#80). The older
    # single-command carve-out was a subset of this; an all-read-only chain now passes too.
    if _is_all_read_only(command):
        return False
    # A `gh ship` release line (plus read-only/`cd` plumbing) is orchestrator-altitude, not
    # implementation — see _is_sanctioned_release_chain (#159).
    if _is_sanctioned_release_chain(command):
        return False
    # A report/verify line (`tg …`, gh PR/CI reads, + read-only/`cd`) is likewise the orchestrator's
    # own altitude — see _is_report_or_verify_chain (coordinator directive).
    if _is_report_or_verify_chain(command):
        return False
    # A chain that merely STARTS with a read-only command (`git status && sed -i ...`,
    # `ls; npm run build`) is judged on its full content, not waved through on its prefix (B1).
    if HEREDOC.search(command) or BUILD_EDIT.search(command):
        return True
    # Multiple chained steps (>= 2 operators, i.e. > 2 commands joined) is implementation-shaped.
    return len(CHAIN.findall(command)) >= 2


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # Subagents do the actual work → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    point = event.get("point") or ""
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    if point == "pre-write":
        offending = _is_code_write(args)
    elif point == "pre-bash":
        offending = _is_implementation_bash(command)
    else:
        offending = False

    if not offending:
        emit("allow")
        return 0

    reason = _override_reason(command, point)
    if reason:
        warn(f"orchestrator work allowed via escape hatch ({reason})")
        emit("allow", f"orchestrator work allowed via escape hatch ({reason})")
        return 0

    # WARN first, BLOCK on repeat within the window.
    if _is_repeat(event):
        emit("block", MESSAGE)
        return BLOCK_EXIT_CODE
    warn(MESSAGE)
    emit("allow", MESSAGE)  # advisory first offense
    return 0


if __name__ == "__main__":
    sys.exit(main())
