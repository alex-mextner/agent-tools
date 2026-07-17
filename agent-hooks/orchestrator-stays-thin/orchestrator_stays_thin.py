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
                git worktree list). ALL `gh` is DELEGATED (Alex tg#7103, reverting the #159/#162
                gh-ship carve-out): `gh ship` AND CI/PR verification (`gh pr checks/view`, `gh
                run`, `gh api`) are a subagent's job now, not inline orchestrator work — they are
                treated as implementation and warn-then-block, exactly like a commit. Commits/
                pushes and test runs (git commit/push, pytest, npm/bun/cargo test) are likewise
                treated as implementation and warn-then-block. A dispatched subagent (agent_id
                present) is exempt and runs gh/ship freely.

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
# `gh` IS DELIBERATELY ABSENT (Alex tg#7103: revert the #159/#162 gh-ship carve-out). The
# orchestrator delegates ALL `gh` to a subagent now — shipping a gated PR (`gh ship`) AND CI/PR
# verification (`gh pr checks/view`, `gh run`, `gh api`) are a subagent's job, not inline
# orchestrator work. `gh` is instead an implementation signal (`_is_gh_command`, defined with the
# other head-normalization helpers), so every gh invocation warn-then-blocks for the orchestrator
# exactly like `git commit`. A dispatched subagent (`agent_id` present) is exempt and runs gh freely
# — the gate governs the orchestrator only. `gh` delegation subsumes the old `gh api` GET-vs-mutation
# distinction: it no longer matters whether a gh call reads or writes; the orchestrator runs no gh.
ORCH_ALLOW = re.compile(
    r"^\s*(?:"
    r"tg\b"                                               # Telegram status reports
    r"|review\b"                                          # multi-model review CLI (read-only)
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
    "time": frozenset(),
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
# build, a raw `git commit`/`git push`, a test run — AND all `gh`, including `gh ship` and CI/PR
# verification). A prior draft framed this as a terse rule ("does not implement inline") that read
# as adversarial and invited bypass attempts. The message now states WHY (the agreed model, not an
# error), HOW to proceed (dispatch a subagent), and that fighting or working around it is out of
# scope. The `gh ship` / read-only-`gh` carve-out (agent-tools#159/#162) was REMOVED here per
# tg#7103: shipping and CI verification are delegated too, so this MESSAGE now DOES fire for an
# inline `gh ship`/`gh pr`/`gh run`/`gh api` by the orchestrator.
MESSAGE = (
    "By design, not a bug: this session is the orchestrator, and the operating model — agreed "
    "with the CTO — is that it never implements inline. Planning, dispatching, and reading "
    "reports back is its job; doing the Edit/Write/Bash itself is not — that includes an edit, a "
    "build, a raw `git commit`/`git push`, a test run, AND all `gh` (shipping a PR with `gh ship` "
    "and CI/PR verification like `gh pr checks`/`gh run` are a subagent's job too, not the "
    "orchestrator's). Dispatch a subagent to do this (Agent tool, run_in_background: true) or "
    "model it as a Workflow, then read its report. This isn't friction to route around — if it's "
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


def _split_chain(command: str) -> list[str]:
    """Split a command into segments on shell operators (``&&`` ``||`` ``;`` ``|`` newline AND a
    bare control ``&``) that lie OUTSIDE quotes. Quote-aware so a ``|`` inside a quoted jq program
    (``jq '.a | .b'``) is NOT a split point — that mis-split used to flap a read chain (codex).

    A bare ``&`` (backgrounding) IS a real segment separator, so `tg done & git commit` splits into
    two segments and the smuggled `git commit` is judged on its own — without this it was one
    segment with a benign `tg` head and slipped past the impl scan (codex review). A redirect ``&``
    (`2>&1`, `&>`, `>&`) and the already-handled `&&` are NOT splits (BG_AMP semantics)."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        prev = command[i - 1] if i > 0 else ""
        nxt = command[i + 1] if i + 1 < n else ""
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
        elif c == "&" and prev not in ("&", ">") and nxt not in ("&", ">"):
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
    """Chain-split (quote-aware) then strip wrappers from each segment (the unit of classification)."""
    return [_strip_wrappers(s) for s in _split_chain(command)]


def _blank_single_quoted(command: str) -> str:
    """Blank the CONTENT of SINGLE-quoted spans only (keeping the quote chars).

    Feeds the substitution scan (`_substitution_inners`), so the blanking must follow shell quote
    SEMANTICS (#164 review P2): inside single quotes everything is literal — a `tg 'saw $(x)'` is
    text, blank it. Inside DOUBLE quotes `$(…)` and backticks still EXECUTE, so double-quoted
    content is kept INTACT for the scan — `gh ship "$(gh api … -X POST)"` must have its inner
    command extracted and judged, not erased. (An earlier version blanked both quote kinds, which
    erased exactly the common quoted-mutation form.) Conservative side effect: a literal `<(…)`
    inside double quotes (where process substitution does NOT execute) is still extracted and may
    over-flag — the safe direction for a gate. Best-effort, matching `_split_chain`:
    backslash-escapes are out of scope (discipline heuristic, not a security boundary).
    SYNC agent-tools#159/#162."""
    out: list[str] = []
    quote: str | None = None
    for c in command:
        if quote is not None:
            if c == quote:
                quote = None
                out.append(c)
            else:
                out.append(" " if quote == "'" else c)
        elif c in ("'", '"'):
            quote = c
            out.append(c)
        else:
            out.append(c)
    return "".join(out)


def _seg_is_allowed(segment: str) -> bool:
    """A single (env-stripped) segment head is read-only inspection OR sanctioned orchestration.

    Head-anchored, so a build/edit token appearing only as an ARGUMENT/needle (`cat tee.log`,
    `git log | rg gh`) stays allowed on its real head, not the needle (the anchor invariant, #5/#80).
    `gh` (any subcommand) is NOT allowed here — it is delegated (`_is_gh_command`); this predicate
    only covers `tg`/`review`/`git worktree list`, read-only inspection, and the `cd` companion.

    A benign head does NOT launder a mutation the head-anchor cannot see: find's mutating primaries
    (delete/exec/file-write) and a `git branch` WITH an argument (its `-D`/create forms) are a read
    head that carries a write, so they forfeit the allow-list (agent-tools#159). `cd` (no repo
    state) is an allowed companion so `cd <repo> && tg 'done' | tail` stays an orchestration chain.
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


def _is_gh_command(segment: str) -> bool:
    """True when a segment's COMMAND head is `gh` (any subcommand) — the orchestrator delegates ALL
    gh (tg#7103). shlex + basename, so a path-qualified head (`/usr/bin/gh ship`) is caught too — the
    SAME normalization `_git_subcommand` applies to git, closing the asymmetry a bare `^gh\\b` regex
    left open (Opus review). `gh` only as an ARGUMENT/needle (`cat gh.md`, `git log | rg gh`,
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
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):  # skip leading VAR=val env-prefixes
        i += 1
    return i < len(toks) and toks[i].rsplit("/", 1)[-1] == "gh"


def _seg_is_impl_signal(segment: str) -> bool:
    """A single (env-stripped) segment is implementation on its own: a build/edit head (sed -i, tee,
    npm/…), a `git commit`/`push` or a `pytest`/`python -m pytest` test (all spellings), a
    `uv run … pytest` wrapper, an unknown `dev` command, OR ANY `gh` command (ship/pr/run/api —
    all delegated, tg#7103). Caught even unchained (codex P1)."""
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
    """True when EVERY (env-stripped) segment is read-only inspection OR sanctioned orchestration.

    The superset used to decide "not implementation": a chain of only `gh pr list`/`gh ship`/`tg`/…
    (or read-only inspection) is orchestration, never blocked. The flapping fix + the `gh ship`
    whitelist (Alex tg#5743 / agent-tools#23).

    Rejects a chain whose substitution smuggles a MUTATION (`gh ship $(gh api -X POST …)`) — the
    head-anchored allow check cannot see inside `$(…)`, so this is the only place to catch it before
    the fast path returns. A BENIGN substitution (`cat $(find …) | grep | head`) is NOT rejected, so
    the #80 read-only-pipe-of-any-length invariant holds (agent-tools#159, Opus review). A bare `&`
    is handled by `_split_chain` splitting it, so its segments are judged individually.
    """
    if HEREDOC.search(command):
        return False
    if _has_mutating_substitution(command):
        return False
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
    # A fully read-only / sanctioned-orchestration chain of ANY length is inspection, never
    # implementation (#80 + the tg#5743 flapping fix). Checked FIRST so a build-token that is only
    # an ARGUMENT of a read-only head (`cat tee.log`) is not mis-flagged by the UNANCHORED BUILD_EDIT
    # needle below — the head-anchored allow-list shields it (#5/#80). `_is_all_inline_allowed`
    # itself rejects a chain whose substitution smuggles a mutation, so the fast path is safe.
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
