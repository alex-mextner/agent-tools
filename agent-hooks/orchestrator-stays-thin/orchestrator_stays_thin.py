#!/usr/bin/env python3
"""agents-hooks/v1 pre-write + pre-bash hook — keep the orchestrator thin.

The orchestrator plans, dispatches, and verifies — it does NOT implement inline. When the
MAIN thread is about to do implementation-shaped work itself (a non-docs code Edit/Write, or
a multi-step implementation Bash) this gate nudges it to delegate to a subagent or a
Workflow. It enforces `delegate-work-to-subagents`.

ONE script binds TWO points via two descriptors; it branches on ``event["point"]``:
  - pre-write : a CODE Edit/Write (non-docs) by the main thread → warn-then-block
  - pre-bash  : a clearly multi-step / implementation-shaped Bash by the main thread →
                warn-then-block. Read-only inspection AND sanctioned ORCHESTRATION are NEVER
                blocked — a single one-liner (git status, ls, cat, grep, find) OR a chain of any
                length whose every segment is read-only OR an orchestration command (tg, review,
                git worktree list). ALMOST ALL `gh` is DELEGATED (Alex tg#7103): CI/PR verification
                (`gh pr checks/view`, `gh run`, `gh api`) and every other gh subcommand are a
                subagent's job, not inline orchestrator work — treated as implementation and
                warn-then-block, exactly like a commit. The ONE exception, restored narrowly (Alex
                tg#9977, agent-tools#159): a genuinely UNCHAINED `gh ship <PR#>` — the gated merge
                itself as the WHOLE, sole command on the line, matched only on that exact shape (a
                bare PR-number argument, no `&&`/`;`/`||`/`|`/bare-`&`/newline anywhere on the
                line) — is sanctioned for the orchestrator to run inline, same as a subagent; every
                other gh shape, including `gh ship` with no PR number, any non-numeric argument, OR
                `gh ship <PR#>` sharing a line with ANYTHING else (even a trailing `| tail` or a
                leading `cd repo &&`), still delegates — the carve-out is a per-LINE grant, not a
                per-segment one (agent-tools#363, closing a `gh ship 205; rm -rf /`-shaped gap a
                per-segment grant left open: see `_is_unchained_gh_ship`'s own docstring). Commits/
                pushes and test runs (git commit/push, pytest, npm/bun/cargo test) are likewise
                treated as implementation and warn-then-block. A dispatched subagent (agent_id
                present) is exempt and runs gh/ship freely regardless.

                `tg` (the tg-cli Telegram reporting tool) is sanctioned for the orchestrator to run
                inline — ALL subcommands, ALL flags, AT ANY CHAIN LENGTH (Alex, direct Telegram
                authorization: "все команды tg-cli" / "ALL tg-cli commands") — this is intentionally
                BROADER than `gh ship`'s single-shape, single-segment carve-out, not the same
                treatment; see `_is_tg_command`'s own docstring for the scope this grants and why it
                stays per-segment where `gh ship` does not. This has been true since agent-tools#164;
                this revision REPLACES the old `ORCH_ALLOW` plain-regex `tg\\b` with `_is_tg_command`,
                a shlex-based predicate (bare `tg` + `VAR=val` env-prefix skip only — deliberately
                NO path-qualification, see the predicate's own docstring for why two review rounds
                landed there) that is now the SOLE authority for the tg allowance and, unlike the
                old regex, correctly rejects a hyphen-suffixed lookalike like `tg-foo`
                (agent-tools#370) — the same laundering-resistance a mutation elsewhere on the line
                still gets (head match only, never a substring/needle; chain-splitting and the
                substitution-liveness scanner still catch a mutation smuggled alongside it).

TIERED (warn → block): the FIRST offense in the TTL window WARNs (allow + message); a REPEAT
in the window BLOCKs. The tier is tracked by a marker file keyed by a hash of cwd. This gives
the doctrine's "WARN then BLOCK" instead of a hard wall on the first inline edit.

Subagent-exempt: a dispatched subagent (``agent_id`` present) does the actual work, so it is
always allowed — this gate governs the orchestrator only.

Per-repo opt-out (Alex tg#5743): default ON; a repo that legitimately works inline on main
(e.g. 3d-cli) sets `agent_hooks.orchestrator_only: false` in its rig.yaml, or exports
RIG_ORCHESTRATOR_ONLY=0. Default ON means an un-enrolled repo keeps the current always-on
behavior (no regression). Mirrors the opt-IN worktree-only-writes guard's per-repo knob.

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="<written justification>"` — when the tiered guard
would BLOCK (a REPEAT offense), the hook asks via a trusted `tg-ctl` and allows ONLY on an
explicit approval tap. A blank value or a bare `1`/`true` is rejected (deny), no Telegram call
is made. An agent can request, not self-grant — the human decides. (This is distinct from the
per-repo ENABLE knob RIG_ORCHESTRATOR_ONLY / agent_hooks.orchestrator_only, which a repo owner,
not the constrained agent, sets to opt a repo out entirely.)

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command (bash) or args.file_path/path (write); event.point
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Iterable

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

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orchestrator-thin")))
# How long a first-offense WARN suppresses the next WARN before a REPEAT becomes a BLOCK.
TTL_S = int(os.environ.get("ORCH_THIN_TTL_S", "900"))

# A write to one of these is documentation, never implementation → always allow.
DOCS_PATH = re.compile(r"\.(?:md|mdx|txt|rst)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)

# Inspection / read-only one-liners that the orchestrator legitimately runs itself.
READ_ONLY_BASH = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|less\b|head\b|tail\b|"
    r"grep\b|rg\b|find\b|pwd\b|echo\b|which\b|env\b|wc\b|stat\b|tree\b|file\b|jq\b|"
    # read-only system-info + text-filter verification tools (SYNC with fix/159 READ_ONLY_BASH):
    # so a verify pipe `df -h | grep /dev | head` / `gh pr view | jq | head` is not blocked by the
    # >=3-segment rule (coordinator: verification is the orchestrator's own altitude).
    r"sort\b|uniq\b|cut\b|column\b|df\b|du\b|lsblk\b|free\b|ps\b|uname\b|"
    r"uptime\b|whoami\b|hostname\b|nproc\b|date\b)"
)
# Sanctioned ORCHESTRATION commands the main thread legitimately runs ITSELF — coordination and
# reporting only. A chain composed entirely of them (or read-only inspection) must never flap into
# a BLOCK. Head-anchored (`^\s*`) exactly like READ_ONLY_BASH so one of these tokens as an ARGUMENT
# (`cat review.log`, `git log | rg tg`) is not waved through (the anchor invariant, #5/#80).
#
# `gh` IS DELIBERATELY ABSENT from THIS head-only regex (Alex tg#7103: revert the #159/#162 gh-ship
# carve-out). The orchestrator delegates almost all `gh` to a subagent — CI/PR verification (`gh pr
# checks/view`, `gh run`, `gh api`) and every other subcommand are a subagent's job, not inline
# orchestrator work. `gh` is instead an implementation signal by default (`_is_gh_command`, defined
# with the other head-normalization helpers), so a generic gh invocation warn-then-blocks for the
# orchestrator exactly like `git commit`. `gh` delegation subsumes the old `gh api` GET-vs-mutation
# distinction: it no longer matters whether a gh call reads or writes; the orchestrator runs no gh
# (except the one exception below). A dispatched subagent (`agent_id` present) is exempt and runs gh
# freely regardless — the gate governs the orchestrator only.
#
# ONE narrow exception, restored (Alex tg#9977, agent-tools#159): a genuinely UNCHAINED
# `gh ship <PR#>` — the WHOLE, sole command on the line — is sanctioned for the orchestrator to run
# inline. It is NOT added to this head-only regex (ORCH_ALLOW has no argument awareness — it cannot
# tell `gh ship 605` from `gh ship` or `gh pr merge`); instead it is its own shlex-based predicate,
# `_is_gh_ship_command`, consulted ONLY by the top-level, whole-LINE `_is_unchained_gh_ship` (NOT by
# `_seg_is_allowed`/`_seg_is_impl_signal` — agent-tools#363 moved it out of those two per-segment
# predicates after a per-segment grant let `gh ship 205; rm -rf /` slip through unblocked; see
# `_is_unchained_gh_ship`'s own docstring for the full story). Every OTHER gh shape — `gh ship` with
# no PR number, `gh pr merge`, `gh run`, `gh pr checks/view`, `gh api`, AND `gh ship <PR#>` chained
# with anything else at all, even a trailing `| tail` — still delegates.
#
# `tg` is DELIBERATELY ABSENT from THIS head-only regex (moved out, tg-carveout-159, Fable review
# round 3): it used to live here as a plain `tg\b` since agent-tools#164, but a plain regex has no
# argument-position awareness and — worse — its `\b` boundary also matches a hyphen-suffixed
# lookalike (`tg-foo`, agent-tools#370). ALL tg-cli commands (Alex, direct Telegram authorization:
# "все команды tg-cli" / "ALL tg-cli commands", explicitly broader than `gh ship`'s single-shape,
# single-segment carve-out — `tg` stays sanctioned at ANY chain length, `gh ship` only when
# unchained) are instead sanctioned by `_is_tg_command`, its own shlex-based predicate (defined with
# the other head-normalization helpers), consulted directly by `_seg_is_allowed` (per-segment, same
# as before) — the SOLE authority for "is this segment `tg`", not a redundant addition alongside a
# regex. `review\b`/`git worktree list` remain here unchanged; `review\b` has the SAME `\b`-boundary
# quirk (`review-foo` also matches) — out of scope for this PR, tracked as the surviving half of
# agent-tools#370.
ORCH_ALLOW = re.compile(
    r"^\s*(?:"
    r"review\b"                                           # multi-model review CLI (read-only)
    r"|git\s+worktree\s+list\b"                           # worktree inspection
    r")"
)
_DEV_ALLOWED_SUBCOMMANDS = frozenset({"start", "run", "list", "status", "logs", "has-script", "stop", "e2e", "env"})
_DEV_ALLOWED_E2E_SUBCOMMANDS = frozenset({"run", "status", "logs", "stop"})
# Intentionally narrower than `dev run <script>` itself: only common test/e2e script names are
# orchestration; other script names remain implementation-shaped so `dev run deploy` is not a bypass.
_DEV_ALLOWED_RUN_SCRIPTS = frozenset({"test", "tests", "e2e", "smoke"})
#
# WRAPPER STRIPPING (`_strip_wrappers`): a leading `VAR=val` assignment or a passthrough command
# wrapper (env / time / timeout / nice / nohup / stdbuf / ionice / setsid, basename-matched so
# `/usr/bin/env` counts) hides the REAL command head — `timeout 60 pytest` (the mandated timeout
# wrapper!), `env git commit`, `CI=1 pytest`, `time git push`, `nice -n10 npm build` — so they are
# stripped before classification. This is a BEST-EFFORT discipline heuristic, NOT a security
# boundary: a determined obfuscation (`env -S 'cmd'`, `sh -c 'cmd'`) can still evade it. The
# sanctioned way to do deliberate inline work is the escape hatch, never evasion. A BARE `env`
# (print environment) is preserved as read-only.
_ASSIGN_RE = re.compile(r"^\w+=")
# Per-wrapper option flags that consume a following OPERAND. Wrapper-SPECIFIC on purpose: the same
# flag means different things per tool (env `-i` = ignore-env, NO operand; stdbuf `-i` = an operand),
# so a shared set would make `env -i git commit` wrongly swallow `git` and bypass the deny (codex).
_WRAPPER_OPT_ARGS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    # `gtimeout` is GNU coreutils `timeout` on macOS (Homebrew) — enforce-timeout-on-bash accepts it
    # as a valid timeout wrapper, so this hook must strip it too, else `gtimeout 60 git commit`
    # slips past implementation detection (codex review).
    "gtimeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "ionice": frozenset({"-c", "-n"}),
    # GNU `time`'s `-f`/`--format` and `-o`/`--output` each consume a following operand
    # (agent-tools#307 review, GitHub bot P1 — pre-existing, reproducible on `main` with NO
    # heredoc at all: `time -f tg pytest`, with an EMPTY operand table for "time", was already
    # misread as the wrapped command being `tg` — because nothing told this stripper that `-f`
    # consumes the very next token — when the REAL wrapped command is `pytest`; `tg` is merely
    # `-f`'s format-string argument. Verified against `main`, unrelated to this PR's heredoc
    # carve-out, but fixed here since it's the same wrapper-stripping mechanism the carve-out
    # reuses and the bug is real regardless of whether a heredoc is involved).
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
}
_WRAPPERS = frozenset(_WRAPPER_OPT_ARGS)
# A `uv run … pytest`/`tox` test wrapper is detected by shlex-parsing (`_is_uv_test`) so the test
# token must be the COMMAND uv runs, not an argument — `uv run rg pytest docs` (a search) is NOT a
# test run. Chain-splitting is quote-aware (`_split_chain`) so a `|` inside a quoted jq program is
# not a split point.
# A command starts at the line start or right after a &&/;/|/( separator. Anchoring the
# build-tool tokens here means the runner must be the COMMAND, not an argument/needle —
# the same anchoring the no-long-inline-process sibling uses (#5).
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"
# A heredoc, or an obvious build/edit invocation, marks implementation-shaped shell.
HEREDOC = re.compile(r"<<-?\s*['\"]?\w+")

# ── agent-tools#307: the ONE provably-safe heredoc shape is carved out of the blanket rule ──
# `$(cat <<'DELIM' ...body... DELIM)` (the `<<-` dash variant and a double-quoted delimiter both
# count too; the delimiter must be a plain `\w+` word — bash also allows a quoted delimiter with
# punctuation/spaces, but that wider form is out of scope of this narrow, easy-to-verify carve-out
# and still hits the blanket block) is inert IN ISOLATION: a QUOTED heredoc delimiter guarantees
# the body is 100% literal shell text — no `$(...)`, backticks, or `$VAR` expansion can occur
# inside it, no matter what the body text looks like — and the only command consuming that body is
# `cat`, which does nothing but echo stdin to stdout. So the substitution's OWN VALUE can only ever
# be a plain string; it cannot execute anything beyond the harmless `cat`.
#
# That is necessary but NOT sufficient — a plain string is only safe if whatever CONSUMES it also
# treats it as inert data (agent-tools#307 review, Codex P1 — TWO review rounds, several distinct
# escapes, every one below verified executing for real, not just reasoned about):
#   - `eval "$(cat <<'EOF'\nBADCMD\nEOF\n)"` — `eval` re-parses its argument as a NEW command.
#   - a nested `tg "$($(cat <<'EOF'\nBADCMD\nEOF\n))"` — a bare `$(<value>)` wrapped around another
#     substitution runs `<value>` as a command (verified: `x=$($(cat <<'EOF'\ntouch /tmp/marker
#     \nEOF\n))` really creates the file).
#   - `tg > "$(cat <<'EOF'\n/etc/passwd\nEOF\n)"` — the substitution is a REDIRECT TARGET, not a
#     plain argument: tg's own stdout gets written to an attacker-chosen path.
#   - `tg ok # $(cat <<'EOF'\ngit commit -m evil\nEOF\n)` — the `$(cat <<'EOF'` sits inside a `#`
#     comment, so real bash NEVER treats it as a heredoc at all; `git commit -m evil` is an
#     ordinary, LIVE, separate command on the next line, not heredoc body (verified: a `#`-hidden
#     `<<'EOF'` lets the following lines execute for real, with only a later, unrelated `EOF`
#     erroring as "command not found" — nothing about that stops the line before it from running).
#   - `eval \` + newline + ` tg "$(cat <<'EOF'\n; BADCMD\nEOF\n)"` — a backslash-newline is a real
#     bash LINE CONTINUATION (removed before parsing), so this is ONE `eval` invocation, not a
#     fresh `tg`-headed segment starting after the newline (verified: `eval \`+newline+` tg_stub
#     "$(cat <<'EOF'\n; echo REALLY_RAN\nEOF\n)"` really runs the smuggled `echo`).
#   - `tg >& "$(cat <<'EOF'\n/path\nEOF\n)"` / `tg > pre/"$(cat <<'EOF'\n../evil\nEOF\n)"` — `>&`/
#     `>|`/zsh `>!` and a word-CONCATENATED target (no whitespace before the substitution) are
#     still one redirect word (verified: `tg >&` really does redirect stdout+stderr to the path).
#   - an escaped `)` inside a nested `eval`'s double-quoted argument, or an ANSI-C `$'...'` opener
#     whose embedded `\'` does NOT end the string (verified: `$'a\'b'` really is the 3-char value
#     `a'b`, unlike plain `'...'`) — each can desync naive bracket/quote counting and either widen
#     an unrelated LATER segment's head match or make substitution depth return to 0 too early.
#     Handling escaping/ANSI-C-quoting correctly everywhere they can appear is equivalent to
#     writing a real shell tokenizer, out of scope for a discipline heuristic — instead, ANY
#     backslash or `$'` sequence anywhere in the segment poisons it outright (no legitimate
#     report needs either).
#   - `tg "$( (:) ; $(cat <<'EOF'\nBADCMD\nEOF\n) )"` / `tg "$(echo ')' ; $(cat <<'EOF'\nBADCMD
#     \nEOF\n))"` — a bare grouping/arithmetic `(` (a subshell or `$((...))`), or a QUOTED `)`
#     inside a nested substitution, both desync naive depth counting: an unmatched bare `(` drops
#     depth by one when only its `)` is counted; a quoted `)` (verified: `echo ')'` prints a
#     literal, inert paren) gets miscounted as a REAL closer by a design that ignores quotes while
#     nested. Both make a genuinely-nested, re-executing heredoc look like it sits at depth 0
#     (verified: both PoCs above really create the file — the `;` makes the inner `$(cat ...)` a
#     standalone command whose plain-string result gets executed). Fixed at the root: quote state
#     is tracked PER SUBSTITUTION NESTING LEVEL (a stack — `_mask_step`), not suspended globally
#     while nested nor ignored altogether — a quoted `)`/`(` is correctly recognized as literal at
#     its OWN level and blanked, while a genuinely bare, unquoted `(` at any level properly pushes
#     a new level (so its matching `)` is accounted for, closing the SAME level it opened) — this
#     also fixes over-blocking a harmless `tg "note (see below)" "$(cat <<'EOF' ...)"`.
#   - `tg-ctl "$(cat <<'EOF'\nfoo\nEOF\n)"` — the consumer-head check used a bare `\b` word
#     boundary (`tg\b`), but `\b` only tests a word/non-word CHARACTER TRANSITION, not "end of
#     token": it matches just as happily right before the `-` in `tg-ctl` as it does before a space,
#     so a heredoc fed to the DIFFERENT command `tg-ctl` (or `tg.sh`, `tg/evil`, anything "tg"
#     followed by punctuation) was wrongly treated as the vetted `tg` (agent-tools#307 review round
#     9, Codex P1). Fixed with an exact-token lookahead (`tg(?=\s|$)`), the same style `CD_HEAD`
#     already uses elsewhere in this file.
#   - `tg"$(cat <<'EOF'\n-ctl\nEOF\n)"` — no whitespace between `tg` and the opening double-quote
#     that wraps the substitution: this is bash word-CONCATENATION again (like the earlier
#     `tg$(cat …)` case), but the word-boundary check used to accept ANY quote character right
#     before the `$(` as proof of a fresh word, without checking what precedes the QUOTE itself —
#     which is identical in both the safe `tg "$(...)"` (space then quote) and unsafe `tg"$(...)"`
#     (no space, quote glued directly onto `tg`) shapes (verified: with a `-ctl` body, the unsafe
#     shape really invokes `tg-ctl`). `_starts_separate_word` fixes this by looking one character
#     further back, past at most one leading quote, for the whitespace that actually matters.
#   - `tg '$(cat <<'EOF'\n' ; printf LIVE_PAYLOAD ; x='\nEOF\n)'` — bash single quotes do not nest,
#     so this is really an alternating sequence of quoted/unquoted spans, with a genuinely LIVE
#     `; printf LIVE_PAYLOAD ;` sitting unquoted in the middle (verified: it really runs). The
#     `$(cat <<'EOF'` shape the regex matched was never a real substitution start at all from
#     bash's perspective — it sits inside an OPEN top-level single-quote, where `$(` is just
#     literal text. `_mask_at_top_level` already tracked `in_single` per level but never surfaced
#     it to the caller; it now does, and the span is rejected whenever it starts inside one.
#   - `tg $(cat <<'EOF'\n--file /etc/passwd\nEOF\n)` — round 10 (Codex P1): NO surrounding double
#     quotes at all. The entire safety argument up to this point implicitly assumed the
#     substitution's result reaches `tg` as exactly ONE argument, but that is only true when it is
#     double-quoted — every legitimate example of this idiom IS (`tg "$(cat <<'EOF' ...)"`). Left
#     unquoted, bash applies its ordinary word-splitting (on IFS) AND filename globbing to the
#     result: a body of `--file /etc/passwd` really becomes the TWO separate argv elements
#     `--file` and `/etc/passwd` (verified with a stub receiving `"$@"`), letting the heredoc body
#     inject arbitrary CLI FLAGS into `tg`'s own invocation — not just literal message text. Fixed
#     by requiring the candidate position to sit INSIDE an open top-level double-quote
#     (`_mask_at_top_level`'s `in_double`, surfaced the same way `in_single` was in the prior fix).
# `review` was DROPPED from the carve-out's consumer set entirely in round 9 (Codex P2): in the
# STANDARD installed configuration, the sibling `no-long-inline-process` hook already intercepts an
# orchestrator-run `review …` at a LOWER priority number (35 vs. this hook's 45). This is a
# "typically true given the standard catalog install" argument, not a structural guarantee (round
# 10, Codex P2): the hook-bridge continues to later descriptors whenever an earlier one ALLOWS
# (fail-open, a hatch-approved exception, or the sibling hook simply not being installed, since each
# agent-hook is independently installable). A dispatched subagent, by contrast, IS unconditionally
# exempt from this whole hook before the carve-out's logic ever runs. So there was no reachable path
# in the common case where collapsing a heredoc fed to `review` actually mattered; it only added
# surface for a benefit that rarely, if ever, materialized.
# So the carve-out ONLY fires when the span is a plain, DOUBLE-QUOTED argument (never a redirect
# target, never inside a comment, never inside an open single-quote, never unquoted) of a segment
# whose (wrapper-stripped, so `timeout 60 tg …`/`env X=1 tg …` still qualify) head is ALREADY a
# sanctioned heredoc consumer — EXACTLY `tg` as its own token, narrower than the full `ORCH_ALLOW`
# (agent-tools#307 review, Codex P2 — `git worktree list` has no documented "safe argument" story)
# — at substitution depth 0, never nested inside another `$(...)`/backtick/`<(`/`>(` at all.
# `_heredoc_span_is_safe_orch_argument` enforces all of this.
# Anywhere else — `eval`/`bash -c`/`source`, a redirect target, a comment-hidden fake heredoc, a
# line-continued disguise, a nested substitution, or any non-sanctioned head — the span is left
# untouched and still hits the ordinary blanket HEREDOC block.
#
# `_strip_safe_heredoc_cat_substitutions` finds every span matching this EXACT shape (in this
# restricted position) and collapses it (the full `$(`...`)`) down to the neutral placeholder
# `$()`, called from `_is_implementation_bash` BEFORE any chain-splitting or classification runs —
# this only removes body noise the parser never tried to interpret anyway; everything else about
# the command (its real head, any OTHER redirect/chain OUTSIDE the collapsed span) is untouched and
# still judged normally. A heredoc that does NOT match this narrow shape — an UNQUOTED delimiter
# (live expansion inside the body), a consuming command other than `cat`, not nested in `$(...)`,
# or a bare heredoc redirected to a file (`cat <<'EOF' > f`) — is likewise left untouched.
_SAFE_HEREDOC_OPEN = re.compile(
    r"\$\([ \t]*\bcat\b[ \t]*<<(-)?[ \t]*(['\"])(\w+)\2[ \t]*\n"
)


def _mask_step(command: str, i: int, stack: list[list[bool]]) -> tuple[str, int]:
    """One character step of `_mask_at_top_level`'s scan. `stack` is a list of `[in_single,
    in_double, in_comment]` triples, one per substitution nesting level (mutated in place: push
    on entering a substitution/subshell, pop on its matching `)`) — see that function's docstring
    for the full per-level rationale. Returns (text_to_emit, new_i).

    The `in_comment` check runs SECOND, right after `in_single` and BEFORE any push logic
    (agent-tools#307 review round 11, Opus: a prior ordering ran the `$(`/backtick/`<(`/`>(` push
    checks first, so a substitution opener sitting inside a `#` comment still pushed a nesting
    level — a push that could never balance, since the `\\n` that ends the comment is consumed at
    the PUSHED level rather than popping it, permanently inflating depth and over-blocking a
    LATER, otherwise-safe heredoc in the same multi-line command — verified: `tg foo # $(
    unbalanced note` followed by a real `tg "$(cat <<'EOF' ...)"` on the next line wrongly hits
    `depth > 0` and gets rejected, even though line 1 is pure comment text in real bash and line 2
    is a valid, collapsible report. Safe-direction only (over-block, never an unsafe allow) but not
    previously documented or tested, so fixed rather than left as an accepted bias.
    """
    c = command[i]
    level = stack[-1]
    in_single, in_double, in_comment = level
    depth = len(stack) - 1
    if in_single:
        if c == "'":
            level[0] = False
        return (c if c == "'" else " "), i + 1
    if in_comment and c != "\n":
        return " ", i + 1
    if command[i:i + 2] == "$(":
        stack.append([False, False, False])
        return command[i:i + 2], i + 2
    if c == "`":
        stack.append([False, False, False])
        return c, i + 1
    if c in ("<", ">") and command[i + 1:i + 2] == "(" and not in_double:
        # `<(`/`>(` process substitution is a real, executing construct UNQUOTED, but bash treats
        # it as plain literal text INSIDE double quotes (verified: `echo "<(echo hi)"` prints the
        # literal text, never expanding it) — unlike `$(...)`/backtick, which still execute inside
        # double quotes either way. So this pushes only when NOT already double-quoted; when
        # double-quoted, falling through to the `in_double` blanking rule below (which blanks a
        # bare `<`/`>`/`(` individually) already gives the correct "treat it as literal" result
        # for free (agent-tools#307 review round 11, Opus).
        stack.append([False, False, False])
        return command[i:i + 2], i + 2
    if in_double and c in (";", "|", "&", "\n", "(", ")", "<", ">"):
        return " ", i + 1
    if c == "\n":
        level[2] = False  # a comment runs only to end of line
        return c, i + 1
    if c == "'" and not in_double:
        level[0] = True
        return c, i + 1
    if c == '"':
        level[1] = not in_double
        return c, i + 1
    if c == "#" and not in_double:
        level[2] = True
        return " ", i + 1
    if c == "(":
        stack.append([False, False, False])
        return c, i + 1
    if c == ")" and depth > 0:
        stack.pop()
        return c, i + 1
    return c, i + 1


def _mask_at_top_level(command: str, end: int) -> tuple[str, int, bool, bool, bool]:
    """Return (masked, depth, in_comment, in_single, in_double) for `command[:end]` — a thin
    driver around `_mask_step`'s per-character state machine.

    Tracks quote/comment state PER SUBSTITUTION NESTING LEVEL — a stack of `[in_single,
    in_double, in_comment]`, one entry per depth — rather than a single global set of flags.
    Each `$(...)`/backtick/`<(`/`>(`/bare `(` grouping is bash's OWN, independent parsing scope,
    so a quote or comment character inside one must be judged against THAT scope's own state,
    never conflated with an outer or sibling scope's (agent-tools#307 review round 7, Opus
    Finding 1: a `)` that is single-quoted text INSIDE a nested `$(...)` was wrongly counted as a
    real closing paren by an earlier "ignore quotes while nested" design, silently returning
    `depth` to 0 early and letting a further-nested, re-executing heredoc collapse — verified:
    `tg "$(echo ')' ; $(cat <<'EOF'\\ntouch /tmp/x\\nEOF\\n))"` really executes the smuggled
    command). Entering a substitution PUSHES a fresh, all-False level; its matching `)` POPS
    back to the parent's own state. A bare `(`/`)`/`;`/`|`/`&`/newline/`<`/`>` INSIDE a level's
    own double-quoted span is blanked (literal there — a quoted `tg "note (see below)"` must not
    desync depth). `$(`/`<(`/`>(`/backtick always push regardless of quoting (they execute even
    inside double quotes); a bare `(`/`#` only push/start-a-comment when NOT double-quoted.

    `in_single` (the TOP-level scope's own `in_single` flag at `end`) matters on its own, not just
    as an input to `depth`/`in_comment`: an OPEN single-quote at `end` means whatever comes next is
    NOT live shell structure at all from bash's point of view — a `$(` immediately after it is
    literal text, not a real substitution start (agent-tools#307 review round 9, Codex P1: a
    candidate `_SAFE_HEREDOC_OPEN` match sitting inside an open top-level single-quote was
    previously accepted anyway, because this function computed `in_single` per-level but never
    surfaced it — verified: `tg '$(cat <<'EOF'\\n' ; printf LIVE_PAYLOAD ; x='\\nEOF\\n)'` really
    runs `printf LIVE_PAYLOAD`, since bash's non-nesting single quotes close and reopen around it,
    turning the `; printf LIVE_PAYLOAD ;` into genuinely live, unquoted top-level text — while the
    classifier's naive textual match still saw a `$(cat <<'EOF'` shape and, without this check,
    collapsed it as if it were the safe, inert construction it merely resembles).

    `in_double` (the TOP-level scope's own `in_double` flag at `end`) is what actually PROVES the
    substitution's result reaches its consumer as exactly ONE argv element (agent-tools#307 review
    round 10, Codex P1): an UNQUOTED `$(...)` is subject to bash's normal word-splitting (on IFS)
    AND filename globbing on its result — `tg $(cat <<'EOF'\\n--file /etc/passwd\\nEOF\\n)` (no
    surrounding quotes) really splits into the TWO separate argv elements `--file` and
    `/etc/passwd`, letting a heredoc body inject arbitrary CLI FLAGS into `tg`'s own invocation
    (verified: a stub receiving `"$@"` really sees two args, not one string) — breaking the entire
    "the substitution can only ever be a plain string" premise, which implicitly assumed exactly
    one resulting argument. Requiring `in_double` at the candidate's position is what the
    documented idiom (`tg "$(cat <<'EOF' ...)"`, always double-quoted in every legitimate example)
    actually relies on for that guarantee, so the carve-out now enforces it explicitly instead of
    merely assuming it.
    """
    out: list[str] = []
    stack: list[list[bool]] = [[False, False, False]]  # [in_single, in_double, in_comment] / depth
    i = 0
    while i < end:
        text, i = _mask_step(command, i, stack)
        out.append(text)
    return "".join(out), len(stack) - 1, stack[-1][2], stack[-1][0], stack[-1][1]


def _scan_segment_signals(masked: str, end: int) -> tuple[int, bool, bool]:
    """Single depth-aware pass over `masked[:end]` (already quote/comment-blanked by
    `_mask_at_top_level`) returning, for the FINAL top-level segment reached at `end`:
    (segment_start, saw_redirect, saw_poison). Full rationale for each signal (verified
    real-bash cases, why `saw_redirect` resets per segment but `saw_poison` never does) lives
    in the module comment block above `_SAFE_HEREDOC_OPEN` — this docstring covers only the
    mechanics:

    - `segment_start`: offset right after the last chain operator (`&&`/`||`/`;`/`|`/bare `&`/
      newline) at depth 0 (mirrors `_split_chain`'s operator set); a backslash-continued `\\n`
      does not count as a boundary.
    - `saw_redirect`: a depth-0 bare `<`/`>` (not starting `<(`/`>(`) since `segment_start` —
      RESETS on each new boundary, so an earlier heredoc's own opener in a DIFFERENT, legitimate
      segment never counts.
    - `saw_poison`: a backslash, a `'` immediately after `$` (an ANSI-C `$'...'` opener), or a
      bare `(` NOT starting `$(`/`<(`/`>(` — ANYWHERE in `masked[:end]`. `_mask_at_top_level`
      already tracks a bare `(` as a real depth-opener via its own per-level stack (so a
      genuinely-nested candidate is already rejected by the `depth > 0` check before this
      function even runs); this is DEFENSE IN DEPTH for THIS function's own simpler, non-stacked
      depth recount — a prior, EARLIER (and now-closed) subshell in the segment can still desync
      just this recount. NEVER resets, because segment-boundary detection itself can be corrupted
      by exactly this signal (agent-tools#307 review rounds 4-8).
    """
    depth = 0
    seg_start = 0
    saw_redirect = saw_poison = False
    i = 0
    while i < end:
        c = masked[i]
        two = masked[i:i + 2]
        prev = masked[i - 1] if i > 0 else ""
        nxt = masked[i + 1] if i + 1 < end else ""
        is_continued_nl = c == "\n" and i > 0 and masked[i - 1] == "\\"
        if depth == 0 and two in ("&&", "||"):
            seg_start, saw_redirect = i + 2, False
            i += 2
            continue
        if depth == 0 and c == "&" and prev not in ("&", ">") and nxt not in ("&", ">"):
            seg_start, saw_redirect = i + 1, False
            i += 1
            continue
        if depth == 0 and c in (";", "|"):
            seg_start, saw_redirect = i + 1, False
            i += 1
            continue
        if depth == 0 and c == "\n" and not is_continued_nl:
            seg_start, saw_redirect = i + 1, False
            i += 1
            continue
        if two == "$(" or (c in ("<", ">") and nxt == "("):
            depth += 1
            i += 2
            continue
        if c == "`":
            depth += 1
            i += 1
            continue
        if c == ")" and depth > 0:
            depth -= 1
            i += 1
            continue
        if c == "\\" or c == "(" or (c == "'" and prev == "$"):
            # NOT gated on depth == 0, and NEVER reset (see the docstring for why) — a backslash,
            # a bare grouping paren, or an ANSI-C `$'...'` opener anywhere poisons the whole scan.
            saw_poison = True
        elif depth == 0 and c in ("<", ">"):
            saw_redirect = True
        i += 1
    return seg_start, saw_redirect, saw_poison


# The heredoc carve-out's own consumer allow-list — deliberately NARROWER than `ORCH_ALLOW`
# (agent-tools#307 review, Codex P2): `git worktree list` is sanctioned ORCHESTRATION (a bare
# inspection command that takes no meaningful argument), but it has no documented "safe argument"
# story the way `tg`'s message body does, so it is excluded here on purpose — the README/descriptor
# only ever promise `tg` collapse, and this constant is what keeps that promise literally true
# rather than accidentally wider via reuse of the whole `ORCH_ALLOW` pattern.
#
# `review` was DROPPED from this set in agent-tools#307 review round 9 (Codex P2): it was dead
# weight, not just unused — in the STANDARD installed configuration, the sibling
# `no-long-inline-process` agent-hook already blocks/warns on an orchestrator-run `review …` at
# priority 35, before this hook's priority 45 runs. This is NOT an absolute guarantee, though
# (agent-tools#307 review round 10, Codex P2): the hook-bridge continues to LATER descriptors
# whenever an earlier one ALLOWS (fail-open, a hatch-approved exception, or the sibling hook simply
# not being installed — each agent-hook is an independently-installable catalog item), so this is a
# "typically true given the standard catalog install" argument, not a structural one. A dispatched
# subagent (the only other caller of a raw `review` invocation) IS fully exempt from THIS hook
# unconditionally, before any of its logic runs at all (`_is_subagent` short-circuits in `main()`).
# So there was no reachable path in the common case where collapsing a heredoc fed to `review`
# changed anything — it only widened the carve-out's surface for a benefit that rarely, if ever,
# materialized. `tg`/`review` no longer share a
# regex at all: EXACT token match only, via a `(?=\\s|$)` lookahead — NOT a bare `\\b` (agent-tools#307
# review round 9, Codex P1): `\\b` is a WORD-TRANSITION test, not an end-of-token test, so `tg\\b`
# wrongly matched inside `tg-ctl`, `tg.sh`, `tg/evil` — any "tg" followed by punctuation rather than
# a letter/digit/underscore (verified: `tg-ctl "$(cat <<'EOF'\\n-ctl\\nEOF\\n)"` really invokes a
# DIFFERENT command, `tg-ctl`, once bash concatenates `tg` with the substitution's `-ctl` value —
# and the old `tg\\b` regex accepted that "tg-ctl" head as if it were plain `tg`). Mirrors the
# `(?=\\s|$)` style `CD_HEAD` already uses elsewhere in this file for exactly the same reason.
_HEREDOC_SAFE_CONSUMER = re.compile(r"^\s*tg(?=\s|$)")


def _heredoc_segment_head_is_safe_consumer(head: str) -> bool:
    """Whether `head` — after stripping a leading `VAR=val`/`env`/`timeout`/… wrapper the SAME way
    `_strip_wrappers` does for every other segment in this file — is EXACTLY `tg` as its own token
    (agent-tools#307 review, Codex P2: `timeout 60 tg "$(cat <<'EOF' ...)"` must not be MORE
    restrictive than the equivalent non-heredoc `timeout 60 tg "plain msg"`, which already passes
    through `_strip_wrappers` elsewhere).

    `head` is a PARTIAL segment slice (up to a heredoc's own `$(`), so it is usually quote-
    UNBALANCED (an open `"`/`'` wrapping the substitution as an argument) and `shlex` can't
    tokenize it directly. Recover by trying `head` as-is, then with its last character (the
    dangling open quote) dropped. If NEITHER shlex-parses, REJECT outright (agent-tools#307
    review round 8, Codex P1) — a prior version fell back to a raw, un-stripped regex check here,
    but a head that's unbalanced even after the one-dangling-quote recovery is exactly the signal
    that something more than a single wrapping quote is going on (verified against real bash: a
    genuinely malformed multi-quote-span construction can hide a live `; echo PWNED ;` between two
    apostrophes that DON'T nest, while the naive regex fallback still saw a leading `tg` and waved
    it through). Every LEGITIMATE shape (`tg "..."`, `tg '...'`, bare `tg $(...)`, wrapped
    `timeout 60 tg "..."`) already parses cleanly via the one-dangling-quote recovery, so this
    never rejects a real report — only genuinely ambiguous quoting.
    """
    for candidate in (head, head[:-1] if head else head):
        if not candidate:
            continue
        try:
            shlex.split(candidate)
        except ValueError:
            continue
        return _HEREDOC_SAFE_CONSUMER.search(_strip_wrappers(candidate)) is not None
    return False


def _starts_separate_word(command: str, pos: int, seg_start: int) -> bool:
    """True iff `pos` begins a genuinely NEW shell word within the segment `command[seg_start:]`,
    rather than being concatenated onto whatever text immediately precedes it.

    Skips AT MOST one immediately-preceding OPENING quote character (`tg "$(...)"` opens its
    argument with a quote, so the quote itself, not the character right before `$(`, is what needs
    to start a fresh word) and then requires either whitespace right before that, or nothing before
    it at all within this segment.

    A quote character alone is NOT sufficient (agent-tools#307 review round 9, Codex P1): a prior
    version accepted `command[pos - 1]` being a quote char unconditionally, which cannot tell
    `tg "$(...)"` (a space, then an opening quote — genuinely a new argument) apart from
    `tg"$(...)"` (NO space — bash concatenates `tg` directly onto the quoted/substituted text,
    forming the single word `tg` + result — verified: with a `-ctl` body, this really invokes the
    DIFFERENT command `tg-ctl`, exactly like the no-quote-at-all word-concatenation case this
    function already had to reject). Both shapes have the SAME character at `pos - 1` (the opening
    quote), so the check must look one character further back, past that quote, to tell them apart.
    """
    j = pos
    if j > seg_start and command[j - 1] in ('"', "'"):
        j -= 1  # the quote itself must start a fresh word -- look past it
    if j <= seg_start:
        return True  # nothing before it in this segment -- it IS the first token
    return command[j - 1] in (" ", "\t", "\n")


def _heredoc_span_is_safe_orch_argument(command: str, pos: int) -> bool:
    """True iff `pos` (a candidate `_SAFE_HEREDOC_OPEN` match start) sits, at substitution depth 0
    (not nested inside ANY other `$(...)`/backtick/`<(`/`>(`), OUTSIDE any `#` comment and OUTSIDE
    any OPEN top-level single-quote, with NO redirect operator and NO backslash anywhere in the
    CURRENT top-level segment (a concatenated target like `tg > path/"$(cat …)"` is still one
    redirect word; a backslash-escaped character defeats bracket/quote counting — see
    `_scan_segment_signals`), inside a segment whose (wrapper-stripped) HEAD is already a
    sanctioned heredoc consumer — EXACTLY `tg` as its own token, narrower than `ORCH_ALLOW`, see
    `_HEREDOC_SAFE_CONSUMER`. This is the gate that closes agent-tools#307's Codex-P1-class
    findings: without it, the carve-out could collapse a heredoc feeding `eval`/`bash -c` (which
    RE-EXECUTES the resulting string), a heredoc used as a REDIRECT TARGET (`tg > "$(cat <<'EOF'
    ...)"`, or word-concatenated onto a literal path prefix, writing to an attacker-chosen
    location), a comment-hidden pseudo-heredoc whose "body" lines are actually real, live, separate
    commands, a backslash-line-continued `eval` whose continuation makes this look like a fresh
    `tg`-headed segment when it is really one `eval` invocation, an EARLIER heredoc's own body
    confusing the quote tracker into WIDENING the head match so a dangerous later segment looks
    safe (fixed at the root for the nested-substitution-quoting class of bypass by
    `_mask_at_top_level` tracking quote state PER SUBSTITUTION NESTING LEVEL — an earlier,
    LEGITIMATE `tg`-headed heredoc argument two positions in the same call still correctly
    collapses; only an actually-dangerous later segment is rejected; NOT an absolute guarantee
    against every cross-body interference, though — a stray UNMATCHED quote in an earlier body can
    still, rarely, over-block a later sibling, a known, tested, safe-direction-only residual, see
    `test_stray_quote_in_earlier_heredoc_body_can_over_block_later_sibling_documented_bias`), an
    escaped paren OR a bare
    grouping/arithmetic `(` defeating depth counting inside a nested substitution (`tg "$( (:) ; $(cat <<'EOF'
    ...))"` — the subshell's own `(`/`)` silently desync our depth counter by -1), a WORD-
    CONCATENATED command name — either bare (`tg$(cat <<'EOF'\n-ctl\nEOF\n)`) or with an opening
    quote directly touching `tg` (`tg"$(cat <<'EOF'\n-ctl\nEOF\n)"`, no space at all — both really
    run `tg-ctl`, a DIFFERENT command, verified against real bash; `_starts_separate_word` is what
    tells these apart from the genuinely-separate-argument shape `tg "$(...)"`), an OPEN top-level
    single-quote at `pos` (`tg '$(cat <<'EOF'\n' ; printf LIVE ; x='\nEOF\n)'` — bash's single
    quotes do NOT nest, so this alternates real quoted spans with genuinely LIVE, unquoted text in
    between; the `$(cat <<'EOF'` shape our regex matched was never a real substitution start at all
    from bash's point of view, since it sits inside an open quote — verified the smuggled command
    really runs), or nested one substitution deep (`$($(cat <<'EOF'...))`, where the OUTER `$(...)`
    runs the inner's plain-string result as a brand-new command), or UNQUOTED (`tg $(cat <<'EOF'
    ...)`, no surrounding double quotes at all — bash word-splits and globs an unquoted
    substitution's result, so a body like `--file /etc/passwd` really becomes the TWO separate
    argv elements `--file` and `/etc/passwd`, letting the heredoc body inject arbitrary CLI FLAGS
    into `tg`'s own invocation; requiring the position to sit INSIDE an open top-level
    double-quote is what actually proves the "exactly one argument" premise the whole carve-out
    depends on) — every one of these verified against real bash, not just reasoned about. This
    combination is the only position where "the substitution can only ever be a plain string" is
    ALSO sufficient, because the thing consuming that string (`tg`, already vetted) never
    re-executes it, never treats it as a redirect target, never has its OWN command name altered
    by it, and receives it as exactly one argument, never word-split or globbed."""
    masked, depth, in_comment, in_single, in_double = _mask_at_top_level(command, pos)
    if in_comment or depth > 0 or in_single or not in_double:
        return False
    seg_start, saw_redirect, saw_poison = _scan_segment_signals(masked, pos)
    if saw_redirect or saw_poison:
        return False
    if not _starts_separate_word(command, pos, seg_start):
        return False  # not a separate word -- concatenated onto whatever precedes it
    return _heredoc_segment_head_is_safe_consumer(command[seg_start:pos])


def _find_heredoc_terminator_end(command: str, start: int, delim: str, dash: bool) -> int | None:
    """The offset of the first line at/after `start` that is EXACTLY the heredoc terminator —
    matching real bash semantics: leading whitespace is stripped from the terminator line ONLY
    under the `<<-` dash variant (tabs), never for a plain `<<`, and NO trailing whitespace is
    tolerated (verified against real bash: a terminator line with a trailing space is NOT
    recognized and the heredoc runs on). None if the command ends before such a line is found (an
    unterminated heredoc — not the safe shape).

    ONE quirk, also verified against real bash: `EOF)` — the delimiter with a `)` immediately
    attached, no space — DOES terminate the heredoc, because closing a `$(...)` needs to find its
    matching paren and bash's lexer treats `)` as ending the terminator word without requiring a
    newline first. When that's the shape, the returned offset points AT the `)` (not past it) so
    the caller's own "optional whitespace then `)`" scan finds the very same paren; a SPACE before
    that `)` breaks the match in real bash too, so it is not accepted here either.

    Getting this wrong in either direction is a correctness bug, not just noise: under-matching
    only leaves body text for the ordinary blanket block to catch (safe), but OVER-matching would
    misidentify the terminator and let the caller collapse too much — absorbing real trailing
    content (a chained `&& git commit`) into the placeholder and silently dropping a mutation from
    judgement. So the terminator line must be matched exactly, not loosely.
    """
    n = len(command)
    pos = start
    while pos <= n:
        scan = pos
        if dash:
            while scan < n and command[scan] == "\t":
                scan += 1
        if command.startswith(delim, scan):
            after = scan + len(delim)
            nxt = command[after] if after < n else ""
            if nxt in ("", "\n"):
                return after + 1 if nxt == "\n" else after
            if nxt == ")":  # same-line quirk — point AT the `)`, ignore what follows it
                return after
        nl = command.find("\n", pos)
        if nl == -1:
            return None
        pos = nl + 1
    return None


def _heredoc_body_has_flag_like_line(body: str) -> bool:
    """True if any LINE of the heredoc `body` text, once its own leading whitespace is stripped,
    starts with `-` (agent-tools#307 review round 11, Codex P1). Even after every other gate in
    this file (exactly one argv element, never re-executed, never a redirect target), the RESULT
    is still just handed to `tg` as literal message text — and `tg` extracts its OWN feature
    flags/options from its argv BEFORE treating anything as message text. A heredoc body that is
    (or contains a line that is) `--help`, `--no-feature ...`, or similar changes what `tg` DOES,
    not just what it prints — an orchestrator-authored `tg --file "$(cat ...)"` choosing to use a
    flag explicitly is one thing, but a heredoc BODY dash-prefixed line reaching `tg` as if it were
    plain report text is a different, narrower thing this carve-out should not paper over. This is
    a cheap, narrow, safe-direction mitigation (rejects the carve-out, falls back to the blanket
    block) — it does not fully resolve tg's own argv-parsing behavior (a separate repo/concern,
    tracked in the agent-tools#307 follow-up ticket), but it closes the most obvious shape."""
    return any(line.lstrip().startswith("-") for line in body.splitlines())


def _strip_safe_heredoc_cat_substitutions(command: str) -> str:
    """Collapse every `$(cat <<'DELIM' ...DELIM...)` span to the neutral placeholder `$()`
    (agent-tools#307 — see the block comment above `_SAFE_HEREDOC_OPEN` for the safety argument).

    Hand-rolled scan, not one big regex: the closing delimiter must be matched by REAL heredoc
    semantics (`_find_heredoc_terminator_end`), and the substitution's closing `)` must
    immediately follow the terminator line (only whitespace/newlines in between — both
    same-line `EOF)` and separate-line `EOF\\n)` are valid heredoc-in-`$()` syntax). AND the span
    must be a plain, DOUBLE-QUOTED argument of an already-`tg`-headed segment at substitution
    depth 0 (`_heredoc_span_is_safe_orch_argument`), AND the body must not contain a dash-prefixed
    line (`_heredoc_body_has_flag_like_line`) — otherwise it is left untouched even though its own
    shape is the safe one, because what CONSUMES the resulting string (`eval`, a nested `$(...)`,
    or `tg`'s own argv-scanning arg parser) is what decides whether that safety actually holds
    (agent-tools#307 Codex P1). Anything that fails any of these checks (more heredoc content
    after the terminator, a second command, an unsanctioned or nested consumer, a flag-like body
    line) is not collapsed and still hits the blanket HEREDOC scan.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _SAFE_HEREDOC_OPEN.search(command, i)
        if not m:
            out.append(command[i:])
            break
        if not _heredoc_span_is_safe_orch_argument(command, m.start()):
            out.append(command[i:m.end()])
            i = m.end()
            continue
        term_end = _find_heredoc_terminator_end(command, m.end(), m.group(3), bool(m.group(1)))
        if term_end is None:
            out.append(command[i:m.end()])
            i = m.end()
            continue
        if _heredoc_body_has_flag_like_line(command[m.end():term_end]):
            out.append(command[i:m.end()])
            i = m.end()
            continue
        paren_m = re.match(r"[ \t\n]*\)", command[term_end:])
        if paren_m is None:
            out.append(command[i:m.end()])
            i = m.end()
            continue
        out.append(command[i:m.start()])
        out.append("$()")
        i = term_end + paren_m.end()
    return "".join(out)


# In-place edit / build invocations. A bare `>`/`>>` redirect is NOT here: a redirect alone
# ("python foo.py > out.log") is not implementation — the in-place editors (sed -i, tee) and
# the build tools are the real signals (B7). The build-tool RUNNERS are anchored at a command
# head so a substring needle in an inspection pipe (`cat notes.md | grep npm`, `git log | rg
# yarn`, `find . -name cargo.toml | wc -l`) is NOT mis-read as implementation (#5). The
# in-place editors (sed -i, tee) keep a bare `\b` anchor: they are a content signal wherever
# they appear (e.g. `git status && sed -i ...`).
# `git commit`/`git push` and the test runners (pytest, go test) are ADDED here (Alex tg#5743):
# the orchestrator must not commit/push or run test suites inline — that is a subagent's job.
# They are anchored at a command head via _CMD_START (npm/bun/cargo already cover `npm test`,
# `bun test`, `cargo test`). `git commit`/`git push` do NOT match READ_ONLY_BASH, so a chain that
# starts read-only (`git status && git commit`) is still judged on its full content and blocks.
BUILD_EDIT = re.compile(
    r"\b(?:sed\s+-i|tee)\b"
    r"|" + _CMD_START + r"(?:npm|pnpm|yarn|bun|cargo|go\s+build|go\s+test|make|"
    r"python\s+setup|pip\s+install|pytest|python\s+-m\s+(?:pytest|unittest)|tox|"
    r"git\s+(?:commit|push))\b"
)
# `uv run … pytest` (the repo's own documented test command) is handled separately by _UV_TEST
# below — targeted so a `uv run` of a read-only tool is not swept in. Other runner wrappers with a
# non-build head are still not caught; the orchestrator is meant to DELEGATE the suite to a subagent
# (which is exempt) anyway, and the tiered warn-then-block + escape hatch cover the residue.
# ── companion-safety vetoes for the sanctioned-orchestration allow-list ──────────────────────
# The allow-list (`_seg_is_allowed`) judges per segment HEAD, so a benign head (`tg`, `review`, a
# read-only inspection) must not launder a mutation hidden elsewhere in the line: a command/process
# substitution (`tg done $(sed -i …)`, `cat <(git push) && tg done`) or a bare background `&`
# (`tg done & git push` — NOT a chain split) would smuggle a mutation past it. These veto the
# free pass wholesale (scanned quote-blanked so a metachar INSIDE a quoted arg does not trip them).
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
# Bare `&` backgrounding. `&&`, `2>&1`, `&>`, `>&` are excluded (not a control `&`).
BG_AMP = re.compile(r"(?<![&>])&(?![&>])")
# `cd` changes no repo state — the one extra companion a sanctioned chain may carry besides
# read-only inspection (`cd <repo> && tg 'done' | tail`). Argv-boundary anchored (`cd(?=\s|$)`),
# NOT `\b`, so `cd-clean` / `cd/foo` are not mistaken for `cd`.
CD_HEAD = re.compile(r"^\s*cd(?=\s|$)")
# find's mutating primaries — the delete/exec family AND the file-WRITE primaries
# (`-fprint`/`-fprintf`/`-fprint0`/`-fls`, which write an arbitrary path) — are a content signal
# anywhere in a segment: a read-only `find` head must not launder them into the allow-list.
FIND_MUTATION = re.compile(r"\s-(?:delete|exec|execdir|ok|okdir|fprintf|fprint0|fprint|fls)\b")

# Rewritten per the CTO's explicit clarification (tg thread, 2026-07-08/#7103): this hook is an
# INTENTIONAL, jointly-accepted operating model — not friction an agent should route around. The
# orchestrator plans and dispatches; a subagent does the implementation-shaped work (an edit, a
# build, a raw `git commit`/`git push`, a test run — AND nearly all `gh`, including CI/PR
# verification). A prior draft framed this as a terse rule ("does not implement inline") that read
# as adversarial and invited unwanted workarounds. The message now states WHY (the agreed model,
# not an error), HOW to proceed (dispatch a subagent), and that fighting it is out of scope. The
# `gh ship` / read-only-`gh` carve-out (agent-tools#159/#162) was REMOVED here per tg#7103
# (shipping and CI verification were both delegated), then PARTIALLY RESTORED (Alex tg#9977,
# agent-tools#159): narrowly, an UNCHAINED `gh ship <PR#>` only (agent-tools#363 narrowed this
# further to a per-LINE grant — see `_is_unchained_gh_ship`). The orchestrator may run that one
# exact, unchained shape inline again, same as a subagent ("the orchestrator CAN gh ship like
# agents can, but BY DEFAULT agents do it"); CI/PR verification, every other gh subcommand, and a
# `gh ship <PR#>` chained with anything else all stay delegated, so this MESSAGE still fires for
# those.
MESSAGE = (
    "By design, not a bug: this session is the orchestrator, and the operating model — agreed "
    "with the CTO — is that it never implements inline. Planning, dispatching, and reading "
    "reports back is its job; doing the Edit/Write/Bash itself is not — that includes an edit, a "
    "build, a raw `git commit`/`git push`, a test run, AND nearly all `gh` (CI/PR verification "
    "like `gh pr checks`/`gh run` are a subagent's job too, not the orchestrator's — an unchained "
    "`gh ship <PR#>`, alone on the line, is the one sanctioned exception, agent-tools#159/#363). "
    "Dispatch a subagent to do this "
    "(Agent tool with `subagent_type: \"fork\"` "
    "or `isolation: \"remote\"` — both run in the background) or model it as a Workflow, then "
    "read its report. This isn't friction to route around — if it's "
    "genuinely wrong for a case, raise that, don't bypass it. There is NO self-service bypass; for "
    "a genuine exception ASK the human, or request a one-time Telegram approval by setting "
    "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN=\"<written justification>\" (deny-by-default; a "
    "bare 1 is rejected). (delegate-work-to-subagents, enforced.)"
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


def _strip_wrappers(segment: str) -> str:
    """Drop leading ``VAR=val`` assignments and passthrough command wrappers (env/time/timeout/…)
    so the REAL command head gets classified (codex). shlex-tokenized with basename matching, so
    `/usr/bin/env git commit` and `timeout 60 pytest` are seen as `git commit` / `pytest`.

    Best-effort: if the strip leaves nothing (a bare `env`) the original is kept (read-only), and a
    determined `env -S 'cmd'` / `sh -c 'cmd'` obfuscation is out of scope (see the module note).
    """
    try:
        toks = shlex.split(segment)
    except ValueError:
        return segment  # unbalanced quotes → don't transform
    i, changed = 0, False
    while i < len(toks):
        if _ASSIGN_RE.match(toks[i]):  # VAR=val
            i += 1
            changed = True
            continue
        base = toks[i].rsplit("/", 1)[-1]  # /usr/bin/env → env
        if base not in _WRAPPERS:
            break
        changed = True
        is_timeout = base in ("timeout", "gtimeout")
        opt_args = _WRAPPER_OPT_ARGS[base]  # operand-taking flags for THIS wrapper only
        i += 1
        while i < len(toks) and toks[i].startswith("-"):
            opt = toks[i]
            i += 1
            if opt in opt_args and i < len(toks):
                i += 1  # this option consumes an operand
        while i < len(toks) and _ASSIGN_RE.match(toks[i]):  # env VAR=val …
            i += 1
        if is_timeout and i < len(toks):
            i += 1  # `timeout`'s first positional is the DURATION; the command follows
    if not changed:
        return segment
    rest = toks[i:]
    return " ".join(shlex.quote(t) for t in rest) if rest else segment


# The characters a backslash actually escapes INSIDE a double-quoted bash string (POSIX): `$`,
# backtick, `"`, `\`, and a literal newline (line continuation). A backslash before any OTHER
# character is literal (kept, not an escape) — e.g. `"\d"` is the two characters `\d`, not `d`.
_DOUBLE_QUOTE_ESCAPABLE = ('"', "\\", "$", "`", "\n")
# Outside any quote, an ANSI-C `'` opener (`$'`), and a bare quote character, can each be escaped
# by a preceding backslash — plus `$` itself (agent-tools#307 review round 13, Codex P1): a `\$`
# means the following `'`, even if adjacent, is a PLAIN single-quote, not an ANSI-C opener, since
# ANSI-C mode requires a genuinely UNESCAPED `$` immediately before the `'`. A literal newline is
# ALSO included (agent-tools#307 review round 14, Opus P2): an unquoted `\`+newline is bash's own
# LINE CONTINUATION, joining two lines into one logical command with no separator at all — a very
# common formatting idiom (`tg exec \` + newline + `pytest`) — so it must not be treated as a bare
# newline (a real segment-boundary character) by `_split_chain`.
#
# `&`, `;`, `|` were added merging agent-tools#363 (rounds 3-4) into this shared parity-aware
# mechanism: a backslash-escaped OPERATOR character outside any quote — `gh ship 605 \' ; rm -rf
# /` (an escaped quote hiding a real `;`) and `gh ship 605 \&& rm -rf /` (an escaped `&` leaving a
# genuinely separate, real `&` right after it) — must also be recognized as a consumed, literal
# unit by `_split_chain`, or the real operator immediately following it is misjudged relative to
# its (now-literal) neighbor. #363's OWN fix used a simpler one-character-lookback `prev_escaped`
# flag scoped to exactly this case; folding it into `_backslash_run`'s general parity walk (rather
# than keeping both mechanisms side by side, which would let two independent consumers each
# process the same backslash run and disagree) covers the same cases through the one engine this
# file now has for backslash handling everywhere.
_UNQUOTED_ESCAPABLE = ("'", '"', "$", "\n", "&", ";", "|")


def _backslash_run(command: str, i: int) -> tuple[int, str, bool]:
    """`command[i]` is a backslash. Returns `(run_end, escaped_char, is_escaped)`: `run_end` is the
    index just past the maximal run of CONSECUTIVE backslashes starting at `i`; `escaped_char` is
    whatever character immediately follows that run (`""` at end of string); `is_escaped` is True
    iff the run's length is ODD.

    Backslash PARITY matters (agent-tools#307 review round 13, Opus/Codex — a prior version only
    ever peeked exactly one character back, so it treated ANY single backslash immediately before
    a quote as escaping it, with no regard for how many backslashes actually preceded that quote):
    in bash, each PAIR of consecutive backslashes collapses to one literal backslash with NO net
    escaping effect on whatever follows — only a single, UNPAIRED (odd-count) backslash is left
    over to escape the next character. So `\\"` (ONE backslash) escapes the quote (verified:
    `tg "a\\""; git commit` really runs the trailing command because the ESCAPED quote does not
    end the string), but `\\\\"` (TWO backslashes) does NOT — the pair cancels out, leaving the
    quote genuinely UNESCAPED, a real opener (verified: `tg \\\\"foo"; git commit` — a DIFFERENT
    real-bash execution than the one-backslash case — still runs `git commit` for real, but via
    the quote actually opening-then-closing normally, not via an escape)."""
    n = len(command)
    j = i
    while j < n and command[j] == "\\":
        j += 1
    escaped_char = command[j] if j < n else ""
    return j, escaped_char, (j - i) % 2 == 1


def _join_continuations(command: str) -> str:
    """Join backslash-newline LINE CONTINUATIONS across the WHOLE command, ONCE, upstream of every
    other check in this file — bash's own first lexical pass (agent-tools#307 review round 17,
    Opus/Fable — UNSAFE direction, a regression `_split_chain`'s own round-15/16 continuation
    handling introduced for a DIFFERENT, earlier check in this file).

    The blanket `HEREDOC` regex (`<<-?\\s*['\"]?\\w+`) — the FOUNDATIONAL catch-all this whole
    carve-out is layered on top of — requires a LITERAL, adjacent `<<` in the RAW command text; it
    has no continuation-awareness of its own and never did. Once `_split_chain` learned to
    correctly REASSEMBLE a continuation-hidden operator for ITS OWN segment-counting purposes, the
    "3+ segments" fallback that used to accidentally catch a continuation-hidden heredoc (via the
    old, continuation-UNAWARE splitter producing MORE, not fewer, segments) stopped being reliable:
    with the body kept minimal (a same-terminator-line, empty-body heredoc), segment count can
    drop to exactly 2, and the write goes completely undetected. Verified against real bash and
    against `main`: `cat <\\` + newline + `<EOF > /tmp/x\\nEOF` really writes `/tmp/x` (empty), but
    `HEREDOC.search` never saw an adjacent `<<` to catch it, and (with this file's OWN
    continuation-reassembly now more accurate than before) the segment-count fallback no longer
    reaches 3 either — an unsafe ALLOW that does not exist on `main`, where the old, continuation-
    unaware splitter produced 3 segments by accident.

    Applying this ONCE, before ANYTHING else (including the heredoc carve-out itself) — rather
    than teaching every individual regex/scanner in this file its own continuation-awareness — is
    also why `_split_chain`/`_blank_single_quoted` keep their OWN, independent continuation
    handling: by the time this function's caller hands them already-joined text, that handling is
    redundant-but-harmless for the standard `_is_implementation_bash` entry point, while remaining
    independently correct for anything that calls them directly (e.g. tests, or a future caller
    that only has a raw fragment, never passed through this join)."""
    out: list[str] = []
    quote: str | None = None
    ansi_c = False
    dollar_available = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        this_dollar_available = dollar_available
        dollar_available = False
        if c == "\\":
            run_end, escaped_char, is_escaped = _backslash_run(command, i)
            consumes = is_escaped and (
                (quote == '"' and escaped_char in _DOUBLE_QUOTE_ESCAPABLE)
                or (quote == "'" and ansi_c and escaped_char in ("'", "\\"))
                or (quote is None and escaped_char in _UNQUOTED_ESCAPABLE)
            )
            if consumes and escaped_char == "\n":
                if run_end - i > 1:
                    out.append(command[i:run_end - 1])
                else:
                    dollar_available = this_dollar_available
                i = run_end + 1
            elif consumes:
                out.append(command[i:run_end + 1])
                i = run_end + 1
            else:
                out.append(command[i:run_end])
                i = run_end
        elif quote is None and c == "$":
            dollar_available = not this_dollar_available
            out.append(c)
            i += 1
        elif quote is not None:
            out.append(c)
            if c == quote:
                quote = None
                ansi_c = False
            i += 1
        elif c in ("'", '"'):
            quote = c
            ansi_c = c == "'" and this_dollar_available
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_chain(command: str) -> list[str]:
    """Split a command into segments on shell operators (``&&`` ``||`` ``;`` ``|`` newline AND a
    bare control ``&``) that lie OUTSIDE quotes. Quote-aware so a ``|`` inside a quoted jq program
    (``jq '.a | .b'``) is NOT a split point — that mis-split used to flap a read chain (codex).

    A bare ``&`` (backgrounding) IS a real segment separator, so `tg done & git commit` splits into
    two segments and the smuggled `git commit` is judged on its own — without this it was one
    segment with a benign `tg` head and slipped past the impl scan (codex review). A redirect ``&``
    (`2>&1`, `&>`, `>&`) and the already-handled `&&` are NOT splits (BG_AMP semantics).

    A backslash-escaped quote INSIDE a double-quoted span does NOT close it (agent-tools#307
    review, GitHub bot P1 — pre-existing, reproducible on `main` with ZERO heredoc involvement:
    `tg "a\\""; git commit` really runs `git commit` in real bash, verified, since `\\"` is a
    literal escaped quote that does not end the string, but the REAL closing `"` right after it
    does; a scanner with no escape awareness closes on the ESCAPED quote instead, then reopens on
    the real one, swallowing the live `; git commit` as if it were still-quoted text). Only
    DOUBLE-quoted spans get this treatment — single quotes have NO escape mechanism at all in
    bash (`'a\\'` is the literal 3 characters `a\\`, verified elsewhere in this file's history),
    so a backslash inside single quotes is ordinary content, never consulted for escaping here.

    An UNQUOTED backslash-escaped quote is the SAME class of bug, one level out (agent-tools#307
    review round 12, Opus): outside any quote at all, a bare `\\"`/`\\'` is bash's OWN way of
    inserting a LITERAL quote character without starting a real quoted span at all (verified:
    `tg \\"a\\"; touch /tmp/marker` really runs the `touch` — the backslash-quote pairs are two
    literal `"` characters, never opening a span, so the `;` right after them is real, live, and
    ends `tg`'s (unquoted) argument).

    ANSI-C `$'...'` quoting is a DIFFERENT quoting mode than a plain `'...'` (agent-tools#307
    review round 12, Codex P1 — UNSAFE direction, not a bias: `tg $'a\\''; git commit` really
    runs `git commit`, verified). Inside `$'...'`, `\\'` is a literal escaped quote that does NOT
    end the string, but a bare `'...'` has NO escape mechanism at all and DOES end on the very
    next `'` — so the SAME character (`'`) means different things depending on whether the quote
    was opened via a preceding, genuinely UNESCAPED `$`.

    All THREE contexts above are escape-PARITY-aware via the shared `_backslash_run` helper
    (agent-tools#307 review round 13, Opus/Codex — the earlier per-context fixes each only ever
    peeked exactly one character back, so `\\\\"` — TWO backslashes, which cancel out to a
    literal backslash with no net escaping effect — was still wrongly treated as escaping the
    quote; `tg \\\\"foo"; git commit` really runs the trailing command in real bash via the quote
    genuinely opening then closing, not via an escape, and the classifier must reach the same
    conclusion by a different mechanism than the one-backslash case). Parity also governs whether
    a `$` immediately before a `'` really starts ANSI-C mode: `tg \\$'a\\'; git commit` escapes
    the `$` itself (one backslash, odd — genuinely escaped), so the `'` that follows is a PLAIN
    single-quote (no escape mechanism), not an ANSI-C opener — verified this also really runs the
    trailing command in real bash, via the plain quote closing after two literal characters.

    The ANSI-C detector tracks `$`-PAIRING via `dollar_available` rather than a positional
    lookback (agent-tools#307 review rounds 14-15, Opus — round 14 fixed `$$` with a check on
    `command[i-2]`, which was itself a regression: it could not tell an ESCAPED `\\$` apart from a
    genuine `$$` pair, wrongly rejecting ANSI-C for `\\$$'...'`, where the escaped first `$` never
    pairs with anything and the second `$` is fresh). Bash pairs consecutive, genuinely UNESCAPED
    `$` characters left-to-right into `$$` (its PID special parameter, a complete 2-char
    expansion): `tg $$'a\\'; git commit` really runs `git commit` (the SECOND `$` pairs with the
    first, so the `'` after it is a PLAIN quote, not ANSI-C), but `tg \\$$'a\\''; git commit` ALSO
    really runs it via a genuinely different mechanism (the first `$` is escaped/literal, so the
    SECOND `$` is fresh and DOES open ANSI-C — verified both against real bash). `dollar_available`
    toggles on each genuinely unescaped `$` (paired → False, fresh → True), correctly handling any
    run length, not just exactly two.

    Merged with `main`'s independently-developed agent-tools#363 fix for this SAME function
    (Fable review round 3, then Opus + Fable review round 4): a BACKSLASH-ESCAPED quote character
    OUTSIDE single quotes does NOT open/close a quoted span — e.g. ``gh ship 605 \\' ; rm -rf /``,
    where bash treats the lone ``\\'`` outside quotes as a literal apostrophe, so the ``;`` that
    follows is a REAL, unescaped command separator (confirmed against `shlex.split`) — and a
    correctly-consumed `\\&` must not leave the check for a REAL, immediately-following ``&``
    thinking it saw an adjacent operator character — e.g. ``gh ship 605 \\&& rm -rf /``, which
    real bash parses as `gh ship 605 &` (backgrounded) followed by a separate `rm -rf /`. #363's
    own fix used a standalone one-character-lookback `prev_escaped` flag scoped to exactly these
    two cases; here both are instead closed by adding the operator characters `&`, `;`, `|` to
    `_UNQUOTED_ESCAPABLE` (see that constant's own comment), so the SAME general `_backslash_run`
    parity engine this function already uses for quote/ANSI-C/`$$`-pairing handling also consumes
    an escaped operator character as one literal unit — one engine, not two independent consumers
    of the same backslash run (which could double-consume it and disagree). Both PRs' regression
    tests — the #307 ANSI-C/parity cases above and the #363 `gh ship 605 \\' ; rm -rf /` /
    `gh ship 605 \\&& rm -rf /` cases — pass against this merged implementation."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    ansi_c = False
    dollar_available = False  # True iff the PRECEDING char is a fresh, unpaired "$" (see below)
    # True iff `command[i - 1]` was just consumed as the ESCAPED target of a backslash run, rather
    # than standing on its own as a real, independently-meaningful character (agent-tools#363,
    # Opus + Fable review round 4, folded into this merge): consuming `\&` as one literal unit (now
    # via `_UNQUOTED_ESCAPABLE` including `&`/`;`/`|`) leaves a genuinely UNESCAPED, standalone `&`
    # immediately after it — e.g. `gh ship 605 \&& rm -rf /` — real bash parses that as
    # `gh ship 605 &` (backgrounded) then a separate `rm -rf /`. The bare-`&` check below excludes
    # a `&` whose `prev` char is also `&` (so it doesn't re-split the tail of an already-consumed
    # `&&`, or misread `2>&1`/`&>` redirects) — but `command[i - 1]` alone can't tell a REAL
    # adjacent `&` apart from one that was just escaped into `buf` as literal content. Without this
    # flag, `prev` reads directly off the raw string and wrongly satisfies that exclusion, merging
    # the genuine standalone `&` into the SAME segment as the escaped one.
    prev_escaped = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        prev = "" if prev_escaped else (command[i - 1] if i > 0 else "")
        prev_escaped = False
        this_dollar_available = dollar_available
        dollar_available = False
        if c == "\\":
            run_end, escaped_char, is_escaped = _backslash_run(command, i)
            consumes = is_escaped and (
                (quote == '"' and escaped_char in _DOUBLE_QUOTE_ESCAPABLE)
                or (quote == "'" and ansi_c and escaped_char in ("'", "\\"))
                or (quote is None and escaped_char in _UNQUOTED_ESCAPABLE)
            )
            if consumes:
                prev_escaped = True
            if consumes and escaped_char == "\n":
                # A LINE CONTINUATION is REMOVED — but ONLY the FINAL backslash + the newline
                # (agent-tools#307 review round 16, Opus — a prior version deleted the WHOLE run
                # wholesale): bash pairs backslashes left-to-right, so in a run of 3+ (always odd,
                # since `is_escaped` already required that), everything BEFORE the last backslash
                # pairs up into literal backslash characters that DO survive — only the trailing,
                # unpaired backslash actually forms the continuation with the newline. Verified:
                # `tg $\\\` + newline + `'a\'; git commit` really runs `git commit` in real bash,
                # via `$` + a literal `\` (from the leading pair) + a PLAIN (non-ANSI-C) quoted
                # `'a\'` — the literal backslash sitting between `$` and `'` means `$` is NOT
                # adjacent to the quote at all, so ANSI-C never opens. `dollar_available` is only
                # carried FORWARD when the run is EXACTLY one backslash (truly invisible removal,
                # nothing else changes) — for a longer run, a literal backslash now sits right
                # before the next position, so `dollar_available` must NOT survive (already False
                # by the per-iteration default, so no explicit reset needed here).
                if run_end - i > 1:
                    buf.append(command[i:run_end - 1])
                else:
                    dollar_available = this_dollar_available
                i = run_end + 1
            elif consumes:
                buf.append(command[i:run_end + 1])
                i = run_end + 1
            else:
                buf.append(command[i:run_end])
                i = run_end
        elif quote is None and c == "$":
            # Bash pairs consecutive, genuinely UNESCAPED `$` characters into `$$` (its own PID
            # special parameter, a complete 2-char expansion) — so whether THIS `$` is "fresh" and
            # available to introduce `$'...'` ANSI-C quoting depends on whether the PRECEDING `$`
            # was itself already available: if so, this one pairs with it (both consumed, neither
            # left over); if not, this one becomes the new available `$` (agent-tools#307 review
            # round 15, Opus — a prior positional-only check, "is the character two back a `$`",
            # could not tell an escaped `\$` (which never pairs with anything) apart from a
            # genuine `$$` pair, wrongly rejecting ANSI-C for `\$$'...'`, where the FIRST `$` is
            # escaped/literal and the SECOND is fresh and does open ANSI-C mode).
            dollar_available = not this_dollar_available
            buf.append(c)
            i += 1
        elif quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
                ansi_c = False
            i += 1
        elif c in ("'", '"'):
            quote = c
            ansi_c = c == "'" and this_dollar_available
            buf.append(c)
            i += 1
        elif command[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
        elif c == "&" and prev not in ("&", ">") and command[i + 1:i + 2] not in ("&", ">"):
            segs.append("".join(buf))  # bare control `&` — a background separator, not a redirect
            buf = []
            i += 1
        elif c in (";", "|", "\n"):
            segs.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    segs.append("".join(buf))
    return [s for s in segs if s.strip()]


def _norm_segments(command: str) -> list[str]:
    """Chain-split (quote-aware; line continuations already removed by `_split_chain` itself) then
    strip wrappers from each segment (the unit of classification).

    A prior version had a SEPARATE `_remove_line_continuations` post-processing step with its own,
    simpler quote-tracker — no escape/ANSI-C/`$$`-pairing awareness at all (agent-tools#307 review
    round 15, Opus/Codex): it got fooled by an unquoted escaped quote (`X=\\' gi<newline>t commit`,
    which really invokes `git`) into thinking it was still "inside a single-quoted span," so it
    never removed the continuation splitting the command name — and, independently, applying it as
    a SEPARATE pass after `_split_chain` already ran meant `_split_chain`'s own `dollar_available`
    tracking got silently reset by the untouched continuation before this fix existed. Folding
    continuation-removal directly into `_split_chain`'s own escape-aware pass (rather than a
    second, independently-fallible parser) closes both classes at once.

    This is NOT the same as saying there is only one such state machine left in the file, though
    (agent-tools#307 review round 16, Fable — a prior version of this docstring overclaimed
    exactly that): `_blank_single_quoted` still carries its OWN, separately-maintained copy of the
    identical quote/escape/`$$`-pairing/continuation logic, kept in sync by hand rather than by
    structural sharing — the repeated rounds 13-16 bug history (fixes landing in one copy but not
    the other, or landing asymmetrically) is the demonstrated cost of that duplication. Properly
    unifying the two into one shared scanner is a larger refactor, tracked as a follow-up
    (agent-tools#310) rather than folded into this already-large change."""
    return [_strip_wrappers(s) for s in _split_chain(command)]


def _blank_single_quoted(command: str) -> str:
    """Blank the CONTENT of SINGLE-quoted spans only (keeping the quote chars).

    Feeds the substitution scan (`_substitution_inners`) and, since agent-tools#363, the
    whole-line `_is_unchained_gh_ship` veto directly — so the blanking must follow shell quote
    SEMANTICS (#164 review P2): inside single quotes everything is literal — a `tg 'saw $(x)'` is
    text, blank it. Inside DOUBLE quotes `$(…)` and backticks still EXECUTE, so double-quoted
    content is kept INTACT for the scan — `gh ship "$(gh api … -X POST)"` must have its inner
    command extracted and judged, not erased. (An earlier version blanked both quote kinds, which
    erased exactly the common quoted-mutation form.) Conservative side effect: a literal `<(…)`
    inside double quotes (where process substitution does NOT execute) is still extracted and may
    over-flag — the safe direction for a gate. SYNC agent-tools#159/#162.

    Escape-aware the SAME way `_split_chain` is (agent-tools#307 review round 12, Codex P1 —
    "backslash-escapes are out of scope" was the PRIOR claim here, but that is now a proven,
    UNSAFE-direction bug, not just a discipline gap): without this, an unquoted `\\'` (a literal
    escaped single-quote, opening no real span in bash) was mistaken for a real single-quote
    OPENER, so a genuinely LIVE, unquoted `$(...)` immediately after it got BLANKED OUT as if it
    were inert single-quoted text — hiding a real mutating substitution from `_has_mutating_
    substitution` entirely (verified: `tg \\' $(git commit)` really runs `git commit`, but the
    prior version of this function blanked the substitution to spaces and the classifier saw
    nothing to flag — an actual laundering vector, not merely an over/under-block nuance). Handles
    both an escaped quote INSIDE an open double-quoted span (which does not close it) and an
    escaped quote OUTSIDE any quote (which does not open one), identically to `_split_chain`.

    ANSI-C `$'...'` quoting (agent-tools#307 review round 12, Codex P1 — UNSAFE direction): the
    SAME `'` character means different things depending on whether it was opened via a preceding,
    genuinely UNESCAPED `$` — inside `$'...'`, `\\'` is a literal escaped quote that does NOT end
    the string, but a bare `'...'` has no escape mechanism and ends on the very next `'`. Without
    this, `tg $'a\'' $(git commit)` closed the ANSI-C string early at the escaped `\\'`, reopened a
    spurious single-quote span at the REAL closing `'`, and blanked the genuinely LIVE
    `$(git commit)` that followed as if it were inert quoted text — hiding it from
    `_has_mutating_substitution` entirely (verified against real bash).

    All THREE contexts are escape-PARITY-aware via the shared `_backslash_run` helper, identically
    to `_split_chain` (agent-tools#307 review round 13, Opus/Codex): `\\\\'` (TWO backslashes,
    cancelling out with no net escaping effect) does NOT escape the quote the way a single `\\'`
    does — `tg \\\\'foo' $(git commit)` really runs the substitution's `git commit` in real bash
    via the quote genuinely opening then closing, not via an escape, and `\\$'a\\'` escapes the `$`
    itself (odd count), so the `'` that follows is a plain single-quote, not an ANSI-C opener.

    The ANSI-C detector tracks `$`-PAIRING via `dollar_available` rather than a positional
    lookback (agent-tools#307 review rounds 14-15, Opus — see `_split_chain`'s docstring for the
    full rationale, including why a round-14 positional fix for `$$` was itself a regression for
    `\\$$'...'`, an escaped-then-fresh `$` pair). `$$'a\\'; git commit` really runs `git commit`
    (the pair consumes both `$`, so the `'` is a plain quote, not ANSI-C — the `\\'` inside it has
    no escape meaning and the string closes at the very next `'`), but `\\$$'a\\''; git commit`
    ALSO really runs it, via a genuinely different mechanism (the escaped first `$` never pairs,
    leaving the second fresh and ANSI-C-opening) — both verified against real bash.

    Also covers agent-tools#363's own concrete bypasses for this function (Opus + Fable review
    round 2/3): `gh ship 605 --note \\' $(rm -rf /)` — an unquoted `\\'` outside any quote must
    not be mistaken for a real single-quote opener, or the genuinely LIVE `$(rm -rf /)` right
    after it gets blanked as if it were inert single-quoted text, hiding a real mutating
    substitution from `_has_mutating_substitution` entirely — already the exact class of bug the
    escape-awareness above closes, so #363's fix and this one converge on the same behavior here
    (unlike `_split_chain`, this function never toggles on `&`/`;`/`|`, so no operator-escaping
    extension was needed for it specifically). `gh ship 605 "it's $(rm -rf /)"` was never actually
    a bug in this function (per #363's own review): it only ever tracks ONE quote type at a time
    and never toggles on `'` while `quote == '"'`."""
    out: list[str] = []
    quote: str | None = None
    ansi_c = False
    dollar_available = False  # True iff the PRECEDING char is a fresh, unpaired "$" (see below)
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        this_dollar_available = dollar_available
        dollar_available = False
        if c == "\\":
            run_end, escaped_char, is_escaped = _backslash_run(command, i)
            consumes = is_escaped and (
                (quote == '"' and escaped_char in _DOUBLE_QUOTE_ESCAPABLE)
                or (quote == "'" and ansi_c and escaped_char in ("'", "\\"))
                or (quote is None and escaped_char in _UNQUOTED_ESCAPABLE)
            )
            if consumes and escaped_char == "\n":
                # Only the FINAL backslash + the newline are removed; leading backslashes in a
                # 3+-run survive (reduced to literal chars, kept INTACT here — this branch only
                # ever fires for `quote == '"'` or `quote is None`, never `"'"`, since ANSI-C's
                # own escape set doesn't include a newline), and `dollar_available` carries
                # forward only for a truly-invisible one-backslash removal — identical treatment
                # to `_split_chain` (agent-tools#307 review round 16, Opus); see that function's
                # docstring for the full rationale.
                #
                # DELIBERATE EXCEPTION to this function's usual length-preservation (every OTHER
                # branch emits exactly as many characters as it consumes — agent-tools#307 review
                # round 16, Opus P3 first raised this as a general invariant to protect): TRUE
                # removal, not same-width space-blanking, is REQUIRED here (round 16, Fable —
                # caught by testing Opus's own suggested fix against real bash before trusting it,
                # not by reasoning alone). `_substitution_inners` scans this function's output with
                # `_SUBST_INNER`, an ADJACENCY-sensitive regex (`\$\(...\)` requires `$` and `(`
                # literally next to each other) — space-blanking the pair leaves `$  (git commit)`
                # in the output, and the regex no longer matches `$(`, so a genuinely LIVE,
                # unquoted `tg "$` + newline + `(git commit)"` substitution (verified against real
                # bash: it really runs `git commit`) went completely undetected. No current caller
                # of this function does position correlation against the original command (only
                # `_substitution_inners`, which reads matched GROUPS, never offsets), so there is
                # no length-preservation contract to actually protect at this specific branch.
                if run_end - i > 1:
                    out.append(command[i:run_end - 1])
                else:
                    dollar_available = this_dollar_available
                i = run_end + 1
            elif quote == '"' and consumes:
                out.append(command[i:run_end + 1])
                i = run_end + 1
            elif quote == "'" and consumes:
                # Unlike the `quote == '"'` case (double-quoted content is kept INTACT since it
                # still executes), an ANSI-C escape pair sits INSIDE a single-quoted span, whose
                # content this function's whole job is to BLANK (agent-tools#307 review round 13,
                # Opus): copying it through verbatim broke that invariant and leaked a bare `'`
                # into the "supposed to be inert" region. Blank the WHOLE run + escaped char,
                # exactly like every other character inside a single-quoted span.
                out.append(" " * (run_end + 1 - i))
                i = run_end + 1
            elif consumes:
                out.append(command[i:run_end + 1])
                i = run_end + 1
            else:
                out.append(command[i:run_end] if quote != "'" else " " * (run_end - i))
                i = run_end
        elif quote is None and c == "$":
            # Same `$$`-pairing/escape-aware tracking as `_split_chain` (agent-tools#307 review
            # round 15, Opus) — see that function's docstring for the full rationale.
            dollar_available = not this_dollar_available
            out.append(c)
            i += 1
        elif quote is not None:
            if c == quote:
                quote = None
                ansi_c = False
                out.append(c)
            else:
                out.append(" " if quote == "'" else c)
            i += 1
        elif c in ("'", '"'):
            quote = c
            ansi_c = c == "'" and this_dollar_available
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _seg_is_allowed(segment: str) -> bool:
    """A single (env-stripped) segment head is read-only inspection OR sanctioned orchestration.

    Head-anchored, so a build/edit token appearing only as an ARGUMENT/needle (`cat tee.log`,
    `git log | rg gh`) stays allowed on its real head, not the needle (the anchor invariant, #5/#80).
    `gh` (ANY subcommand, INCLUDING `ship`) is NOT allowed here — it is delegated (`_is_gh_command`);
    this predicate covers `review`/`git worktree list` (via `ORCH_ALLOW`), `tg` (via its own
    shlex-based `_is_tg_command`), read-only inspection, and the `cd` companion.

    `gh ship <PR#>` is DELIBERATELY ABSENT from this per-segment allow-list (agent-tools#363,
    reverting a #159/#162 regression): the ship carve-out is granted ONLY by
    `_is_unchained_gh_ship` at the whole-LINE level, in `_is_all_inline_allowed`, for a genuinely
    single-segment `gh ship <PR#>` command — never per-segment here. A prior revision consulted
    `_is_gh_ship_command` directly in this OR-chain, which made EVERY segment matching the ship
    shape allowed regardless of chain position; combined with `_seg_is_impl_signal`'s matching
    per-segment exception, that let `gh ship 205; rm -rf /` (or any companion this file has no
    named pattern for — `chmod`, `scp`, `mv`, `dd`, …) sail through silently: the ship segment
    passed the allow-list, the unrecognized companion matched no impl-signal regex either, and a
    2-segment chain doesn't reach the `>= 3` chain-length fallback. Reverting to a per-LINE grant
    closes that gap: the moment `gh ship <PR#>` sits in ANY multi-segment chain, `_seg_is_allowed`
    denies it here exactly as it denies any other `gh` subcommand, so the chain is judged on its
    OTHER segments' own merits (or the `_seg_is_impl_signal` trip below), not waved through by a
    recognized-pattern allow-list that cannot enumerate every possible mutation.

    A benign head does NOT launder a mutation the head-anchor cannot see: find's mutating primaries
    (delete/exec/file-write) and a `git branch` WITH an argument (its `-D`/create forms) are a read
    head that carries a write, so they forfeit the allow-list (agent-tools#159). `cd` (no repo
    state) is an allowed companion so `cd <repo> && tg 'done' | tail` stays an orchestration chain.

    Deliberately NOT gated on `BUILD_EDIT`/other impl-signal regexes THIS FUNCTION DOESN'T ITSELF
    ENFORCE: a sanctioned head/shape (`tg`, `review`, …) is allowed here even when its OWN argument
    TEXT happens to match a mutation-shaped regex this predicate has no explicit veto for (`tg 'saw
    $(git push) in logs'` — `BUILD_EDIT` is never checked by THIS function at all) — that contract
    is "does this segment's HEAD/SHAPE look sanctioned", not "does this segment's text contain no
    mutation-shaped substring anywhere THIS FUNCTION SCANS FOR". It is NOT a blanket "sanctioned
    shape always wins": the two vetoes THIS FUNCTION DOES check (`FIND_MUTATION`, `git branch
    <arg>`) still forfeit the allow-list even for an otherwise-sanctioned segment. For genuine
    mutations THIS function has no veto for at all, the REAL protection is elsewhere and
    unconditional: chain-splitting (a real `&&`/`;`/`|`/bare-`&` makes it a SEPARATE segment, judged
    on its own) and the substitution-liveness scanner (a LIVE `$()`/backtick/`<()`/`>()` executes
    and is caught by `_has_mutating_substitution` regardless of what this predicate says about the
    outer segment).
    """
    if FIND_MUTATION.search(segment):
        return False
    if _git_subcommand(segment) == "branch" and _git_branch_has_arg(segment):
        return False
    if (
        READ_ONLY_BASH.search(segment)
        or ORCH_ALLOW.search(segment)
        or _dev_segment_is_allowed(segment)
        or CD_HEAD.match(segment)
        or _is_tg_command(segment)
    ):
        return True
    return _git_subcommand(segment) in _GIT_READ_SUBS  # `git -C d status`, `/usr/bin/git log`, …


# The gh head as a plain regex, robust to an UNBALANCED-quote segment that shlex cannot parse:
# optional leading `VAR=val` env-prefixes (`_strip_wrappers` also bails on the bad quote, so they
# survive), then an optional path prefix (`/usr/bin/`), then `gh` at an argv boundary (`(?=\s|$)`,
# NOT `\b`, so `gh-foo` is not `gh`). Mirrors the old `_GH_API_HEAD` regex's quote-robustness. A
# command-WRAPPER (`timeout 60 gh …`) combined with an unbalanced quote is out of scope — best-effort
# discipline, and bash rejects an unbalanced quote before the command ever runs (see module note).
_GH_HEAD_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*(?:\S*/)?gh(?=\s|$)")


def _gh_argv_after_env(toks: list[str]) -> list[str] | None:
    """Skip leading `VAR=val` env-prefix tokens, then confirm the (now-first) token is `gh`
    (basename-normalized, e.g. `/usr/bin/gh` -> `gh`). Returns the argv AFTER `gh` (an empty list
    for a bare `gh` with no subcommand), or `None` when the env-stripped head is not `gh`.

    Shared by `_is_gh_command` and `_is_gh_ship_command` so BOTH the env-skip AND the basename
    check are written ONCE (Fable review: an earlier version of this helper skipped env-prefixes
    only, leaving the basename check duplicated in both callers — a drift risk this closes). Each
    caller still does its OWN try/except around this, because the two need DIFFERENT behavior on
    an unparseable segment (deny falls back to a conservative head regex; the ship carve-out just
    says no)."""
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "gh":
        return None
    return toks[i + 1:]


def _is_gh_command(segment: str) -> bool:
    """True when a segment's COMMAND head is `gh` (any subcommand) — the orchestrator delegates
    ALMOST ALL gh (tg#7103); a caller needing the ONE narrow exception (`gh ship <PR#>`,
    agent-tools#159) consults `_is_gh_ship_command` separately, this predicate alone does not
    carve it out. shlex + basename, so a path-qualified head (`/usr/bin/gh ship`) is caught too —
    the SAME normalization `_git_subcommand` applies to git, closing the asymmetry a bare `^gh\\b`
    regex left open (Opus review). `gh` only as an ARGUMENT/needle (`cat gh.md`, `git log | rg gh`,
    `grep 'gh ship' log`) is NOT a gh command: only `toks[0]` (the head) is inspected.

    Leading `VAR=val` env-prefixes are skipped in BOTH branches so the predicate is self-contained
    and symmetric — a caller that forgets to `_strip_wrappers` first still classifies
    `GH_PAGER=cat gh ship` correctly (the shlex and unbalanced-quote paths agreed only after this;
    Opus review). On an UNBALANCED-quote segment shlex cannot parse, fall back to the head regex
    rather than returning False — the old `gh api` path was CONSERVATIVE here (block on unparseable),
    so `gh ship 605 'oops` must still register as gh (the safe direction for a delegation gate)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return bool(_GH_HEAD_RE.match(segment))  # unbalanced quotes → conservative head match
    return _gh_argv_after_env(toks) is not None


# A bare PR number: ASCII digits only (never a unicode digit look-alike, never a URL/branch name).
# `\Z` (not `$`) so a trailing newline in the token can't sneak a match (Opus review) — a discipline
# heuristic detail, not exploitable (any real newline in the raw command chain-splits first), but
# the stricter anchor matches intent in a security-relevant predicate.
_PR_NUMBER_RE = re.compile(r"^[0-9]+\Z")
def _is_gh_ship_command(segment: str) -> bool:
    """True when a segment's COMMAND is narrowly `gh ship <PR#>` — the ONE gh mutation carved back
    out for the orchestrator (agent-tools#159, restored; Alex tg#9977: "the orchestrator CAN gh
    ship like agents can, but BY DEFAULT agents do it"). Deliberately narrow: the head must be
    `gh` (basename/env-prefix normalized exactly like `_is_gh_command`, via the shared
    `_gh_argv_after_env`), the very next token must be the literal `ship`, and the token after THAT
    must be a bare PR number (ASCII digits only, non-empty). `gh ship` alone (no PR number),
    `gh ship --help`, `gh ship abc`, and every OTHER gh subcommand (`gh pr merge`, `gh run`,
    `gh pr checks`/`view`, `gh api`, …) do NOT match and fall through to `_is_gh_command`'s general
    (still-delegated) deny.

    PURE ARGV-SHAPE predicate, deliberately chain-unaware — it says nothing about WHERE `segment`
    sits on the command line. `_seg_is_allowed`/`_seg_is_impl_signal` do NOT consult this directly
    (agent-tools#363): the actual carve-out gate is `_is_unchained_gh_ship`, which wraps this call
    with the whole-LINE "is there only one segment at all" check that decides whether the ship
    exception actually applies. See that function's docstring for why a per-segment consultation of
    THIS predicate alone used to be the security gap.

    Anything AFTER the PR number — flags (`--screenshot`, `--repo`, even `--skip-ci`), a trailing
    redirect token (`2>&1`) — is unrestricted: this predicate recognizes the ship *shape* only and
    does not vet ship's own flags. `--skip-ci` in particular has its OWN separate,
    independently-gated live-Telegram hatch inside `ci/ship/ship.sh` (`RIG_HATCH_REQUEST_SHIP_SKIP_CI`)
    that this gate does not duplicate — an inline `gh ship 605 --skip-ci` passes THIS gate the same
    as any other ship, and ship.sh's own gate is the sole control on the skip-ci bypass itself.

    Basename/env-prefix normalization intentionally mirrors `_is_gh_command`'s DENY-side behavior
    reused on the ALLOW side (Fable review flagged the asymmetry: a deny-side conservatism becomes
    permissive when reused for a grant, e.g. a `gh` binary at a nonstandard path or under an
    unrelated env-prefix also qualifies). Accepted as consistent with this module's stated threat
    model — a cooperative orchestrator, not an adversarial one; `on_error: open` and the whole gate
    are discipline, not a security boundary (see the module docstring) — rather than diverging the
    two predicates' head-matching and risking the drift this shared helper exists to prevent.

    On an UNPARSEABLE (unbalanced-quote) segment, shlex raises — return False (not recognized as
    ship) rather than guessing, so the segment falls through to `_is_gh_command`'s OWN conservative
    head-regex fallback and stays delegated. This is the mirror image of `_is_gh_command`'s own
    unbalanced-quote fallback (which blocks): the safe default for GRANTING an allowance is "no
    match", while the safe default for a DENY is "match" — same direction of caution, opposite
    boolean outcome."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    argv = _gh_argv_after_env(toks)  # None if the (env-stripped) head isn't `gh`
    if argv is None:
        return False
    return len(argv) >= 2 and argv[0] == "ship" and bool(_PR_NUMBER_RE.match(argv[1]))


def _is_unchained_gh_ship(command: str) -> bool:
    """True when the ENTIRE command line — including any TRAILING separator, not only an internal
    one — is free of `&&`/`||`/`;`/`|`/bare-`&`/newline, free of a heredoc, and free of a LIVE
    substitution, whose (wrapper-stripped) content is exactly `gh ship <PR#>`
    (`_is_gh_ship_command`). THE sole grant point for the ship carve-out (agent-tools#363): a
    per-LINE grant for a genuinely unchained, single-purpose invocation, not a per-segment one.

    This is deliberately a top-level, whole-command check rather than something `_seg_is_allowed`/
    `_seg_is_impl_signal` decide per segment. The PRIOR design (agent-tools#159/#162, restored)
    wired the ship exception into those two per-segment predicates directly, which made EVERY
    segment matching the ship shape exempt regardless of where it sat in a chain — so `gh ship
    205; rm -rf /` passed silently: the ship segment was allowed, `rm -rf /` matched no impl-signal
    regex this file recognizes (BUILD_EDIT/uv-test/git-python-impl/dev/gh are all narrow,
    named-pattern lists — they cannot enumerate every destructive command), and a 2-segment chain
    never reaches the `>= 3` chain-length fallback. Gating the grant on "is this line free of every
    separator" closes THAT specific hole — a `gh ship <PR#>` sharing a line with anything else,
    even a single trailing `| tail` or a leading `cd repo &&`, returns False here, `gh` reverts to
    being an ordinary (still-delegated) impl-signal via `_seg_is_impl_signal`, and the WHOLE line
    is judged by the pre-#159 rules: a `gh` segment anywhere trips it, independent of what the
    other segments are or whether THEY happen to match a recognized mutation pattern. It does NOT
    close the broader, pre-existing "an unrecognized head with no impl-signal in a <3-segment
    chain silently falls through to allowed" behavior that this whole file has always had for
    every OTHER sanctioned head (`tg`, `review`, read-only inspection) — see the module's own
    per-segment allow-list docs and the README's "Known, PRE-EXISTING gap" note for that boundary;
    fixing it is a separate, unscoped change this PR deliberately does not make (Fable review,
    round 2: an earlier draft of this docstring said the fix closes the gap "unconditionally",
    which overstated it — it closes it for lines a `gh ship` grant would otherwise have waved
    through, not for the file's fall-through-to-allowed default in general).

    SELF-CONTAINED on HEREDOC/substitution, not delegated to caller discipline (Opus + Fable
    review, round 2 of #363): an earlier revision relied on `_is_all_inline_allowed` checking
    `HEREDOC`/`_has_mutating_substitution` BEFORE ever consulting this function, documented only
    as a "callers MUST" docstring warning. That left an identical hole ONE LEVEL DOWN from the
    chain-operator gap this function exists to close: `gh ship 605 $(rm -rf /)` is a single
    segment (`_split_chain` sees no `;`/`&&`/…), so it reached this function; `_is_gh_ship_command`
    matches (`605` is clean, everything after — including the whole `$(rm -rf /)` token — is
    unrestricted trailing text by design); and `_has_mutating_substitution`'s OWN narrow-pattern
    scan does not recognize `rm -rf /` as a mutation any more than `_seg_is_impl_signal` recognizes
    it chained — the exact same "unenumerable companion" flaw, reached via `$()` instead of `;`.
    Rather than trying to enumerate every destructive command a substitution could carry (the
    unenumerable-list problem this whole fix exists to get away from), this function now refuses
    the grant on ANY live substitution marker at all — `$(`, a backtick, `<(`, or `>(` (process
    substitution executes too, same as command substitution) — found OUTSIDE single quotes
    (scanned via `SUBSTITUTION` against the single-quote-blanked command, the same quote-semantics
    `_substitution_inners` uses: a literal `$(...)` inside single quotes never executes and does
    not disqualify the grant, e.g. `gh ship 605 --note 'ran $(build) earlier'` stays allowed; a
    live one inside DOUBLE quotes still executes and IS caught, including when an unrelated
    apostrophe sits inside the same double-quoted span, e.g. `gh ship 605 "it's $(rm -rf /)"` —
    `_blank_single_quoted` only enters blanking mode on a `'` seen OUTSIDE an open `"..."` span, so
    the apostrophe here never toggles quote state and the live `$(...)` stays visible to the scan).
    This also makes the function itself the sole, self-contained authority its docstring already
    claimed it was — a future caller that consults it directly, without first replicating
    `_is_all_inline_allowed`'s HEREDOC/substitution ordering, gets the same safe answer, instead of
    silently reopening this class of gap.

    TRAILING separators disqualify too, not only internal ones (Fable review, round 2): a bare
    `gh ship 605;` or `gh ship 605 &` has no companion command after the separator, so
    `_split_chain` drops the resulting empty segment and `len(segs) == 1` alone would still call
    it unchained — contradicting this docstring's own "no separator anywhere" contract and, worse,
    silently BACKGROUNDING the sanctioned merge for the trailing-`&` case (the orchestrator would
    lose the synchronous exit status of the one mutation it is allowed to run inline — the same
    backgrounding concern the sibling `subagent-no-bg-longproc` hook polices elsewhere). Checking
    `segs[0] == command` (not just `len(segs) == 1`) catches this: `_split_chain` only returns the
    ORIGINAL string unchanged, byte-for-byte, when it found zero separators to split on; any real
    separator — leading, trailing, or internal — changes what comes back, even after empty-segment
    filtering collapses the segment COUNT back down to one."""
    if HEREDOC.search(command):
        return False
    if SUBSTITUTION.search(_blank_single_quoted(command)):
        return False
    segs = _split_chain(command)
    return len(segs) == 1 and segs[0] == command and _is_gh_ship_command(_strip_wrappers(segs[0]))


def _is_tg_command(segment: str) -> bool:
    """True when a segment's COMMAND head is `tg` (the tg-cli Telegram reporting tool) — ANY
    subcommand, ANY flag combination is sanctioned: plain text, `--file`, `--photo`, `--format
    html`, `--tag`, `--reply-to`, `voice setup`, and everything else tg-cli exposes. Authorized
    explicitly and broadly by Alex (Telegram, direct reply, this thread): "оркестратор ещё и tg:*
    должен уметь, т.е. все команды tg-cli" — "the orchestrator should ALSO be able to run tg:*,
    i.e. ALL tg-cli commands". Unlike `gh ship <PR#>`'s PR-number-shape narrowing, this predicate
    does NOT narrow by subcommand or argument position: tg is a leaf reporting/notification tool
    with no subcommand this gate has reason to withhold from the orchestrator.

    THE SOLE authority for "is this segment `tg`" (tg-carveout-159, Fable review round 3) —
    `ORCH_ALLOW` used to carry a plain `tg\\b` regex alternative for this (agent-tools#164); that
    regex is now REMOVED from `ORCH_ALLOW` and this predicate replaces it outright, not sitting
    alongside it as a redundant addition (an earlier draft of this PR kept both — Fable correctly
    flagged that `ORCH_ALLOW` being checked FIRST in `_seg_is_allowed`'s OR-chain made this
    predicate unreachable dead code end-to-end, since the plain regex matched a strict superset of
    what it could ever grant). Consolidating ALSO fixes agent-tools#370 for the `tg` case: `\\b` is
    a WORD-boundary, which also fires before a hyphen, so the old regex incorrectly matched
    `tg-foo` too — this predicate's basename-EXACT token check (`toks[i] == "tg"`) does not have
    that quirk, and now that it is the only gate, `tg-foo` is correctly rejected end-to-end (see
    `test_tg_foo_is_now_rejected_end_to_end`, which supersedes the pinned-quirk test an earlier
    draft had). `review`'s matching `\\b` quirk (`review-foo`) is UNCHANGED and out of scope here —
    the surviving half of agent-tools#370.

    shlex-based, with a self-contained `VAR=val` env-prefix skip (matching `_is_gh_command`'s own
    precedent, so a caller that invokes this predicate directly, without going through
    `_strip_wrappers` first, still classifies `TG_BOT_TOKEN=x tg 'msg'` correctly).

    **Deliberately NO path-qualification** (`/opt/homebrew/bin/tg`), despite `_is_gh_command`
    accepting any path — resolved across two review rounds, not shipped as a guess. `_is_gh_command`
    is a DENY-direction predicate (over-matching there only routes more things to a subagent, the
    safe failure mode); this predicate is GRANT-direction (a match means "never even warn"), so the
    same blanket path normalization would over-match in the UNSAFE direction — a relative path
    (`./tg`) is trivial to place anywhere near the working directory, and narrowing to "absolute
    paths only" does not actually narrow anything real (`/tmp/tg` is just as easy to write as
    `./tg` — disproved empirically in round 2). A hardcoded bin-dir allowlist was also rejected: it
    would be unreliable in practice (this predicate's own authoring machine resolves its real `tg`
    to a non-standard, dotfiles-managed path, not any short "standard" list). Given this gate's own
    stated threat model (discipline, not a security boundary — see the module docstring's
    "Fail-open, on purpose") and that the practical need (Alex's ask) is fully met by subcommand
    breadth alone, path-qualification was dropped entirely.

    Deliberately does NOT exclude argument text that happens to look like a mutation (`tg 'ran
    sed -i on it'`) — the SAME "sanctioned shape wins over argument text this function doesn't scan
    for" precedent `review` already has (see `_seg_is_allowed`'s own docstring). `gh ship <PR#>`
    used to share this exact per-segment precedent too (agent-tools#159), but agent-tools#363 moved
    its grant to the whole-LINE `_is_unchained_gh_ship` instead — see
    `test_incidental_mutation_shaped_text_in_a_ship_segment_matches_tg_precedent`, which still pins
    the `gh ship` case but now documents the DIFFERENT (line-level, not segment-level) mechanism
    that grants it. The real protection against a genuine mutation is unconditional and elsewhere:
    chain-splitting (a real `&&`/`;`/`|`/bare-`&` makes it a SEPARATE segment) and the
    substitution-liveness scanner (a LIVE `$()`/backtick/`<()`/`>()` executes and is caught
    regardless of what this predicate says about the outer segment).

    On an UNPARSEABLE (unbalanced-quote) segment, shlex raises — return `False`, mirroring
    `_is_gh_ship_command`'s conservative-fallback DIRECTION (deny-by-default; a discipline gate
    should not grant on an input it cannot confidently classify), NOT the old regex's "recognize
    it anyway" fallback. That old direction was justified ONLY by parity with the (now-removed)
    `tg\\b` regex, which had no notion of "unparseable" at all; now that this predicate is the sole
    authority rather than a redundant addition, the parity argument no longer applies, and the safe
    default for a GRANT-direction predicate facing uncertain input is to not grant (Opus/Fable
    round 3: decide the fallback direction on its own merits once the predicate is load-bearing,
    not by inheriting an old regex's behavior)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    return i < len(toks) and toks[i] == "tg"


def _seg_is_impl_signal(segment: str) -> bool:
    """A single (env-stripped) segment is implementation on its own: a build/edit head (sed -i, tee,
    npm/…), a `git commit`/`push` or a `pytest`/`python -m pytest` test (all spellings), a
    `uv run … pytest` wrapper, an unknown `dev` command, OR ANY `gh` command at all (pr/run/api/ship
    — delegated, tg#7103). Caught even unchained (codex P1).

    `gh ship <PR#>` has NO exception here (agent-tools#363, reverting a #159/#162 regression): a
    prior revision special-cased it (`_is_gh_command(segment) and not _is_gh_ship_command(segment)`)
    so a ship segment was never a trip signal regardless of chain position — combined with the same
    exception in `_seg_is_allowed`, that let a chained `gh ship 205; rm -rf /` slip through, since
    neither the ship segment (allow-listed) nor the unrecognized `rm -rf /` companion (matches no
    impl-signal regex this file has a pattern for) ever tripped, and a 2-segment chain never reaches
    the `>= 3` chain-length fallback either. The carve-out now lives EXCLUSIVELY in
    `_is_unchained_gh_ship`, consulted by `_is_all_inline_allowed` at the whole-line level, for a
    genuinely single-segment `gh ship <PR#>` command — never here. This function's job is simpler
    and matches its PRE-#159 form: ANY `gh` head, ship included, is a trip signal, so a `gh ship`
    segment sharing a line with anything else makes the WHOLE line implementation-shaped, exactly
    like every other `gh` subcommand."""
    if (
        BUILD_EDIT.search(segment)
        or _is_uv_test(segment)
        or _is_git_or_python_impl(segment)
        or _dev_segment_is_unknown(segment)
    ):
        return True
    return _is_gh_command(segment)


def _dev_tokens(segment: str) -> list[str] | None:
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0].rsplit("/", 1)[-1] != "dev":
        return None
    return toks


def _dev_segment_is_allowed(segment: str) -> bool:
    toks = _dev_tokens(segment)
    if not toks:
        return False
    if len(toks) == 1 or toks[1] in ("-h", "--help"):
        return True
    if toks[1] == "run":
        args = toks[2:]
        while args and args[0] == "--repo-only":
            args = args[1:]
        return bool(args) and (args[0] in _DEV_ALLOWED_RUN_SCRIPTS or args[0] in ("-h", "--help"))
    if toks[1] == "e2e":
        return len(toks) == 2 or toks[2] in _DEV_ALLOWED_E2E_SUBCOMMANDS or toks[2] in ("-h", "--help")
    return toks[1] in _DEV_ALLOWED_SUBCOMMANDS


def _dev_segment_is_unknown(segment: str) -> bool:
    toks = _dev_tokens(segment)
    return toks is not None and not _dev_segment_is_allowed(segment)


# git global options that take a SEPARATE operand (so `git -C /repo commit` finds `commit`, not `/repo`).
_GIT_GLOBAL_OPT_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)
_GIT_READ_SUBS = frozenset({"status", "log", "diff", "show", "branch"})


def _git_subcommand(segment: str) -> str | None:
    """The git subcommand after skipping global options, or None if the segment isn't `git`.

    shlex + basename, so it sees the subcommand across spellings the head regexes miss:
    path-qualified (`/usr/bin/git`) and global options before the subcommand (`git -C d status`,
    `git -c k=v commit`, `git --git-dir=.git commit`) (codex round 8). Used by BOTH the read
    carve-out and the mutation deny so the two never disagree on what a git command is.
    """
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0].rsplit("/", 1)[-1] != "git":
        return None
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):  # attached --opt=val has no operand
            i += 1
    return toks[i] if i < len(toks) else None


def _git_branch_has_arg(segment: str) -> bool:
    """True when a `git branch` segment carries ANY argument. `git branch` bare is a read-only
    LIST; `git branch <arg>` covers the mutating forms (`-D`/`-d`/`-m`/`-M`/create) AND read flags
    like `-a`/`-v` — conservatively ALL forfeit the allow-list (a lone read `git branch -a` still
    passes via the `< 3 segments` fallback). shlex + global-option aware like `_git_subcommand`, so
    the `-C` form (`git -C /repo branch -D`) is covered too. agent-tools#159."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return True  # unparseable → do not grant the allow-list
    if not toks or toks[0].rsplit("/", 1)[-1] != "git":
        return False
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):
            i += 1
    if i >= len(toks) or toks[i] != "branch":
        return False
    return i + 1 < len(toks)  # any token after `branch`


def _is_git_or_python_impl(segment: str) -> bool:
    """shlex-based detection of `git commit`/`git push` and `pytest`/`python -m pytest|unittest`
    across spellings the head regex misses: path-qualified (`/usr/bin/git`, `/usr/bin/python3`),
    `python3`/`pythonX.Y`, and git GLOBAL options before the subcommand (codex round 8)."""
    if _git_subcommand(segment) in ("commit", "push"):
        return True
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    if not toks:
        return False
    cmd = toks[0].rsplit("/", 1)[-1]  # basename
    if re.fullmatch(r"python3?(?:\.\d+)?", cmd):
        return toks[1:3] in (["-m", "pytest"], ["-m", "unittest"])
    return cmd in ("pytest", "tox")


_UV_TEST_SHORT_FLAGS_WITH_VALUE = frozenset({"C", "P", "b", "c", "f", "i", "p", "w"})
# Keep these uv option tables in sync with dev-cli's dev_cli/cli.py _UV_RUN_* copies
# (the `dev` CLI was extracted from agent-tools into the standalone alex-mextner/dev-cli repo).
_UV_TEST_BOOLEAN_FLAGS = frozenset({
    "--active",
    "--all-extras",
    "--all-groups",
    "--all-packages",
    "--compile-bytecode",
    "--exact",
    "--frozen",
    "--gui-script",
    "--help",
    "--isolated",
    "--locked",
    "--managed-python",
    "--module",
    "--native-tls",
    "--no-build",
    "--no-build-isolation",
    "--no-binary",
    "--no-cache",
    "--no-config",
    "--no-default-groups",
    "--no-dev",
    "--no-editable",
    "--no-env-file",
    "--no-index",
    "--no-managed-python",
    "--no-progress",
    "--no-project",
    "--no-python-downloads",
    "--no-sources",
    "--no-sync",
    "--offline",
    "--only-dev",
    "--refresh",
    "--reinstall",
    "--script",
    "--upgrade",
    "-U",
    "-h",
    "-m",
    "-n",
    "-q",
    "-s",
    "-v",
})
_UV_TEST_FLAGS_WITH_VALUE = frozenset({
    "--allow-insecure-host",
    "--build-constraint",
    "--cache-dir",
    "--color",
    "--config-file",
    "--config-setting",
    "--config-settings-package",
    "--constraint",
    "--default-index",
    "--directory",
    "--env-file",
    "--exclude-newer",
    "--exclude-newer-package",
    "--extra",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--group",
    "--index",
    "--index-strategy",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--no-binary-package",
    "--no-build-isolation-package",
    "--no-build-package",
    "--no-extra",
    "--no-group",
    "--only-group",
    "--package",
    "--prerelease",
    "--project",
    "--python",
    "--python-preference",
    "--python-platform",
    "--refresh-package",
    "--reinstall-package",
    "--resolution",
    "--upgrade-package",
    "--with",
    "--with-editable",
    "--with-requirements",
})


def _is_uv_test(segment: str) -> bool:
    """True when a segment is a `uv run … pytest`/`tox`/`python -m pytest|unittest` TEST run.

    shlex-parsed so the test token must be the COMMAND uv runs (after `uv run` + its options), not
    an argument — `uv run rg pytest docs` (searching for "pytest") is NOT a test run (codex)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    if len(toks) < 3 or toks[0] != "uv" or toks[1] != "run":
        return False
    for i in _uv_test_payload_starts(toks, 2):
        if i >= len(toks):
            continue
        cmd = toks[i]
        if cmd in ("pytest", "tox"):
            return True
        if cmd == "python" and toks[i + 1:i + 3] in (["-m", "pytest"], ["-m", "unittest"]):
            return True
    return False


def _uv_test_payload_starts(toks: list[str], index: int) -> Iterable[int]:
    pending = [index]
    visited: set[int] = set()
    yielded: set[int] = set()
    while pending:
        cursor = min(pending.pop(), len(toks))
        if cursor in visited:
            continue
        visited.add(cursor)
        while cursor < len(toks):
            opt = toks[cursor]
            if opt == "--":
                cursor += 1
                break
            if opt == "-" or not opt.startswith("-"):
                break
            if opt.startswith("--"):
                flag = opt.split("=", 1)[0]
                if "=" in opt:
                    cursor += 1
                elif flag in _UV_TEST_FLAGS_WITH_VALUE:
                    cursor += 2
                elif flag in _UV_TEST_BOOLEAN_FLAGS:
                    cursor += 1
                else:
                    pending.append(cursor + 2)
                    cursor += 1
            elif _uv_test_short_flag_takes_following_value(opt):
                cursor += 2
            elif _uv_test_short_flag_has_attached_value(opt):
                cursor += 1
            elif _uv_test_short_flag_is_known_boolean(opt):
                cursor += 1
            else:
                pending.append(cursor + 2)
                cursor += 1
        cursor = min(cursor, len(toks))
        if cursor not in yielded:
            yielded.add(cursor)
            yield cursor


def _uv_test_short_flag_takes_following_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    for pos, flag in enumerate(flags):
        if flag in _UV_TEST_SHORT_FLAGS_WITH_VALUE:
            return pos == len(flags) - 1
    return False


def _uv_test_short_flag_has_attached_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    return any(
        flag in _UV_TEST_SHORT_FLAGS_WITH_VALUE and pos < len(flags) - 1
        for pos, flag in enumerate(flags)
    )


def _uv_test_short_flag_is_known_boolean(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    return all(f"-{flag}" in _UV_TEST_BOOLEAN_FLAGS for flag in token[1:])


def _is_all_read_only(command: str) -> bool:
    """True when EVERY (env-stripped) chain segment head is a read-only inspection command.

    A fully read-only pipe (`find ... | grep ... | head`) is the orchestrator's bread-and-butter
    inspection and must never be blocked, no matter how many segments it has (#80).
    """
    if HEREDOC.search(command):
        return False
    segs = _norm_segments(command)
    return bool(segs) and all(READ_ONLY_BASH.search(s) for s in segs)


def _is_all_inline_allowed(command: str) -> bool:
    """True when EVERY (env-stripped) segment is read-only inspection OR sanctioned orchestration,
    OR the whole line is a genuinely unchained `gh ship <PR#>` (`_is_unchained_gh_ship`).

    The superset used to decide "not implementation": a chain of only `gh pr list`/`tg`/… (or
    read-only inspection) is orchestration, never blocked. The flapping fix (Alex tg#5743 /
    agent-tools#23).

    Rejects a chain whose substitution smuggles a MUTATION (`tg done $(gh api -X POST …)`) — the
    head-anchored allow check cannot see inside `$(…)`, so this is the only place to catch it before
    the fast path returns. A BENIGN substitution (`cat $(find …) | grep | head`) is NOT rejected, so
    the #80 read-only-pipe-of-any-length invariant holds (agent-tools#159, Opus review). A bare `&`
    is handled by `_split_chain` splitting it, so its segments are judged individually.

    The `_is_unchained_gh_ship` check sits AFTER these two vetoes, but — unlike an earlier revision
    of this comment claimed — that ordering is no longer LOAD-BEARING (Fable review, round 2 of
    #363): `_is_unchained_gh_ship` is now self-contained and independently refuses HEREDOC and any
    live substitution itself (see its own docstring), so `gh ship 605 $(sed -i x)` is caught either
    way — by this function's own veto above, OR by `_is_unchained_gh_ship`'s internal one, should a
    future edit ever reorder these three checks. The ordering here is kept for readability (vetoes
    before grants) and as defense-in-depth, not because correctness depends on it.

    NOT a "does this line contain at least one orchestration head" check (tg-carveout-159, Opus +
    Fable review round 4 both independently misread it that way): `all(_seg_is_allowed(s) for s in
    segs)` is a per-segment universal check with no minimum-count or specific-source requirement —
    a chain whose ONLY orchestration head is `_is_tg_command`-matched (not `ORCH_ALLOW`-matched),
    or a chain that is PURELY read-only with zero orchestration heads at all, both satisfy this the
    SAME way. Moving `tg` out of `ORCH_ALLOW` and into its own predicate therefore does not change
    which chains get the any-length exemption — every `_seg_is_allowed` source (`READ_ONLY_BASH`,
    `ORCH_ALLOW`, `_is_tg_command`, `_dev_segment_is_allowed`, `CD_HEAD`) contributes equally to
    this `all(...)`. `gh ship <PR#>` is deliberately NOT one of those sources any more
    (agent-tools#363) — it is granted only by the separate `_is_unchained_gh_ship` check below,
    which is per-LINE, not per-segment; see that function's own docstring for why.
    """
    if HEREDOC.search(command):
        return False
    if _has_mutating_substitution(command):
        return False
    if _is_unchained_gh_ship(command):
        return True
    segs = _norm_segments(command)
    return bool(segs) and all(_seg_is_allowed(s) for s in segs)


# A single-level substitution — `$(…)`, backticks, `<(…)`/`>(…)` — capturing its INNER command.
# One level (no nested parens) on purpose: a discipline heuristic, not a shell parser.
_SUBST_INNER = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)")


def _substitution_inners(command: str) -> list[str]:
    """Inner commands of the LIVE substitutions in ``command``. Scanned on a copy with only
    SINGLE-quoted spans blanked: single-quoted text is literal (never a substitution), but a
    substitution inside DOUBLE quotes still EXECUTES (`gh ship "$(gh api … -X POST)"`) and must be
    extracted and judged (#164 review P2). Lets a mutation smuggled in a substitution head-position
    (`gh ship $(gh api -X POST …)`) still be judged: the outer segment head is `gh ship`, so only
    scanning the inner catches the `gh api` mutation (codex review)."""
    inners: list[str] = []
    for m in _SUBST_INNER.finditer(_blank_single_quoted(command)):
        inner = next((g for g in m.groups() if g), "")
        if inner.strip():
            inners.append(inner)
    return inners


def _has_mutating_substitution(command: str) -> bool:
    """True when an UNQUOTED substitution's own command is implementation — a mutation smuggled
    where no segment head can see it (`gh ship $(gh api -X POST …)`, `cat $(git commit)`). A BENIGN
    substitution (`cat $(find …)`) is not, so the #80 read-only-pipe invariant holds (Opus review)."""
    return any(_seg_is_impl_signal(s)
               for inner in _substitution_inners(command)
               for s in _norm_segments(inner))


def _is_implementation_bash(command: str) -> bool:
    if not command.strip():
        return False
    # Join line continuations ONCE, first, before ANYTHING else — including the heredoc carve-out
    # below (agent-tools#307 review round 17, Opus/Fable): the blanket `HEREDOC` regex a few lines
    # down requires a LITERAL, adjacent `<<`, so a continuation hiding it (`cat <\` + newline +
    # `<EOF > /tmp/x`) must be reassembled before that check runs, or the write goes undetected
    # entirely. Same defensive try/except pattern as the heredoc carve-out below: any internal
    # failure here must fail toward the SAFE direction (leave `command` untouched, still hit the
    # ordinary blanket checks on the un-joined text) rather than propagate and crash the whole gate
    # open under `on_error: open`.
    try:
        command = _join_continuations(command)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure here must not widen ALLOW
        warn(f"continuation-joining raised {exc!r} — leaving command untouched (safe direction)")
    # agent-tools#307: collapse the ONE provably-safe heredoc shape — `$(cat <<'DELIM' ...
    # DELIM)`, ONLY when it is a plain argument of an already-`tg`-headed segment at substitution
    # depth 0 (never nested, never feeding `eval`/`bash -c`/anything else) — to the neutral
    # placeholder `$()` before anything else runs, so every downstream check in THIS function, and
    # in `_is_all_inline_allowed` (which it calls), judges the command as if that inert span were a
    # plain string. A heredoc that does not match this exact, restricted shape is left untouched
    # here and still hits the blanket block below.
    #
    # This is the newest, most complex parsing code in the file, so it gets its OWN defensive
    # try/except (agent-tools#307 review round 9, Fable): on ANY internal exception, fall back to
    # the ORIGINAL, unmodified command rather than letting the exception propagate — which would
    # otherwise crash this hook process entirely and, under `on_error: open`, fail the WHOLE gate
    # open (allow) rather than just leaving this one heredoc for the ordinary blanket block to
    # catch. This keeps a bug in the new carve-out failing in the SAFE direction (still blocked by
    # HEREDOC below) instead of the unsafe one (allowed by an unrelated crash). The `warn()` call
    # (agent-tools#307 review round 10, Fable) gives this path actual observability: silently
    # swallowing with a bare `pass` would make a SYSTEMATIC bug in the new parser (one that raises
    # on every input, not just a crafted one) indistinguishable from "the carve-out just never
    # matches" — both look like permanent over-blocking with zero diagnostic signal either way.
    try:
        command = _strip_safe_heredoc_cat_substitutions(command)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure here must not widen ALLOW
        warn(f"heredoc carve-out raised {exc!r} — leaving command untouched (safe direction)")
    # A fully read-only / sanctioned-orchestration chain of ANY length is inspection, never
    # implementation (#80 + the tg#5743 flapping fix). Checked FIRST so a build-token that is only
    # an ARGUMENT of a read-only head (`cat tee.log`) is not mis-flagged by the UNANCHORED BUILD_EDIT
    # needle below — the head-anchored allow-list shields it (#5/#80). `_is_all_inline_allowed`
    # itself rejects a chain whose substitution smuggles a mutation, so the fast path is safe.
    # ORDERING IS LOAD-BEARING for the unchained `gh ship <PR#>` grant (agent-tools#363, Opus
    # review round 5): `_seg_is_impl_signal("gh ship 605")` is unconditionally True (no per-segment
    # ship exception any more), so a bare `gh ship 605` is allowed ONLY because this check runs
    # BEFORE the impl-signal scan below. Do not reorder these two checks.
    if _is_all_inline_allowed(command):
        return False
    if HEREDOC.search(command):
        return True
    # A build/edit head or a gh api mutation in ANY (env-stripped) segment is implementation — even
    # a single unchained one (`git commit`, `env pytest`, `gh api -X POST`). `_split_chain` splits a
    # bare `&`, so `tg done & git commit` is judged on its `git commit` segment.
    if any(_seg_is_impl_signal(s) for s in _norm_segments(command)):
        return True
    # ...and a mutation smuggled inside a substitution head-position, which no segment head sees.
    if _has_mutating_substitution(command):
        return True
    # Otherwise, multiple chained steps (>= 3 segments, i.e. >= 2 operators) is impl-shaped. Counted
    # from the quote-aware split so a `|` inside a quoted arg is not counted as an operator.
    return len(_split_chain(command)) >= 3


# ── per-repo opt-out (rig.yaml) ─────────────────────────────────────────────────────────
# SYNC: the two helpers below are duplicated in
# agent-hooks/worktree-only-writes/worktree_only_writes.py (each agent-hook dir is deliberately
# self-contained so it installs standalone — no shared import). Keep the parse logic identical.

def _find_rig_yaml(cwd: str) -> Path | None:
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None)."""
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _target_dir(cwd: str, args: dict) -> str:
    """The dir whose rig.yaml governs a WRITE — the target file's dir, else ``cwd``.

    Mirrors worktree-only-writes._target_dir (SYNC): a pre-write's opt-out must follow the checkout
    the FILE lives in, not the shell cwd (they can differ — an absolute write into another repo).
    The file may not exist yet (a create) → walk up to the nearest existing ancestor.
    """
    base = cwd or os.getcwd()
    if not isinstance(args, dict):
        return base
    for key in ("file_path", "path", "notebook_path"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            target = Path(raw)
            if not target.is_absolute():
                target = Path(base) / target
            for cand in (target.parent, *target.parent.parents):
                if cand.exists():
                    return str(cand)
            return base
    return base


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Read the boolean ``agent_hooks.<key>`` from rig.yaml text — deliberately minimal parse.

    Stdlib-only (no PyYAML): find the top-level ``agent_hooks:`` block (indent 0) and read the
    key ONLY as a DIRECT child of it — a deeper-nested ``<key>:`` (e.g. under ``items.<hook>``)
    must NOT flip the guard repo-wide. A malformed/absent key returns the default (fail-open) — a
    parse miss only means "keep the default", the safe direction for a discipline gate.
    """
    in_block = False
    child_indent: int | None = None  # the indent of agent_hooks' DIRECT children
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()  # drop trailing comment (values here are booleans)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_block = head == "agent_hooks"
            child_indent = None
            continue
        if not in_block:
            continue
        if child_indent is None:
            child_indent = indent  # first child fixes the direct-child level
        if indent != child_indent:
            continue  # deeper-nested key (items.<hook>.<k>) → not the block-level knob
        if head == key and ":" in line.strip():
            val = line.strip().split(":", 1)[1].strip().strip("\"'").lower()
            if val in ("true", "yes", "on", "1"):
                return True
            if val in ("false", "no", "off", "0"):
                return False
            return default  # empty (`key:`) or unrecognized value → the default (codex P2)
    return default


def _orchestrator_only_enabled(cwd: str) -> bool:
    """Whether the thin-orchestrator gate is ON for this repo. env override > rig.yaml > ON.

    Default ON (opt-OUT), unlike the opt-IN worktree-only guard: this gate has always been
    always-on, so an un-enrolled repo must keep firing (no regression). Any falsy
    `RIG_ORCHESTRATOR_ONLY` (`0`/`false`/`no`/`off`) or `agent_hooks.orchestrator_only: false`
    exempts a repo (e.g. 3d-cli). The env falsy set matches the rig.yaml one so `=false` behaves
    like the YAML `false` — an env-only `!= "0"` check surprised users who set `=false` (Opus review).
    """
    env = os.environ.get("RIG_ORCHESTRATOR_ONLY")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    root = _find_rig_yaml(cwd)
    if root is None:
        return True
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return True
    return _agent_hooks_bool(text, "orchestrator_only", default=True)


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
    cwd = str(event.get("cwd") or "")

    # Per-repo opt-out (Alex tg#5743): a repo that legitimately does inline work on main
    # (e.g. 3d-cli) sets `agent_hooks.orchestrator_only: false` in its rig.yaml. Default ON, so an
    # un-enrolled repo keeps the current always-on behavior (no regression). For a pre-write the
    # governing repo is the TARGET file's, not the shell cwd (codex) — they can differ.
    cfg_dir = _target_dir(cwd, args) if point == "pre-write" else cwd
    if not _orchestrator_only_enabled(cfg_dir):
        emit("allow")
        return 0

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

    # WARN first, BLOCK on repeat within the window. Only a would-be BLOCK consults the hatch —
    # a first-offense WARN is advisory (allow) and needs no escalation.
    if _is_repeat(event):
        ctx = {"hook": "orchestrator-stays-thin", "point": point, "command": command}
        # Resolve the hatch config (rig.yaml / tg_ctl_path) from the GOVERNING repo — for a
        # pre-write that is the TARGET file's repo (`cfg_dir`), which can differ from the shell
        # `cwd`; using `cwd` would route/deny an approval for a cross-repo write via the wrong
        # repo's config. The inline `RIG_HATCH_REQUEST_*=` form only exists for the pre-bash
        # point (a pre-write has no shell command to carry it — the var must be exported there).
        hatch = hatch_escalation.request_hatch_approval(
            "orchestrator-stays-thin",
            ctx,
            cwd=cfg_dir,
            command=command if point == "pre-bash" else None,
        )
        if hatch.should_stop:
            if hatch.approved:
                warn(f"orchestrator-stays-thin allowed via hatch escalation ({hatch.reason})")
                emit("allow", f"allowed via hatch escalation ({hatch.reason})")
                return 0
            emit("block", f"hatch escalation denied: {hatch.reason}\n{MESSAGE}")
            return BLOCK_EXIT_CODE
        emit("block", MESSAGE)
        return BLOCK_EXIT_CODE
    warn(MESSAGE)
    emit("allow", MESSAGE)  # advisory first offense
    return 0


if __name__ == "__main__":
    sys.exit(main())
