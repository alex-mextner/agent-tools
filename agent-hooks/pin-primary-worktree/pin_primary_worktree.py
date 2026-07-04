"""agents-hooks/v1 pre-bash hook — pin the repo's PRIMARY worktree to its default branch.

Incident this closes (Alex tg#6462/tg#6477, 2026-07-04): an agent doing HYP-917 work ran
`git checkout feat/hyp-autofix-unsupported-framework` in the SHARED main checkout
(/Users/ultra/work/hyperide) instead of its own isolated worktree. No file damage happened
(the agent caught it immediately and had authored everything in its real worktree), but a
SECOND concurrent agent then also checked out (and committed on) a different feature branch
in that same shared checkout before switching back to main — a real collision waiting to
happen, since any agent relying on the primary checkout sitting on main can have the rug
pulled from under it mid-operation.

`worktree-only-writes` (the sibling pre-write gate) already denies an Edit/Write while the
checkout sits on the default branch — but it never sees a bare `git checkout <branch>` (not
an Edit/Write tool call), and once the checkout HAS moved to a feature branch, that gate's own
logic treats "on a feature branch" as "exactly where authoring belongs" — true for a linked
worktree, false for the primary one. Neither gate distinguishes "which worktree is this" from
"which branch is checked out", which is exactly the blind spot this incident exploited.

This hook closes that gap directly: it inspects `git checkout` / `git switch` invocations and
BLOCKS one that would move the PRIMARY worktree (never a `git worktree add`-created linked
worktree — see `is_primary_worktree`) onto anything other than the repo's default branch.
Checking OUT of a feature branch back to default, `git worktree add` (a different worktree
entirely), `git merge`/`git pull`/`git fetch`, and any checkout inside a LINKED worktree are
all unaffected. Complements (does not replace) `worktree-only-writes`.

PER-REPO, opt-in — reuses the SAME `agent_hooks.worktree_only` knob as `worktree-only-writes`
(one feature, one flag; see that hook's docstring for the resolution order: env
RIG_WORKTREE_ONLY > rig.yaml agent_hooks.worktree_only > default OFF).

Escape hatch (mirrors worktree-only-writes): RIG_ALLOW_MAIN_EDIT=1 (+ optional
RIG_ALLOW_MAIN_EDIT_REASON) allows the one-off deliberate checkout.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command/cmd (the bash string); event.cwd is the shell dir.
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": workflow discipline, not a security boundary — any git/parse failure (or
an ambiguous target this hook cannot resolve, e.g. `git checkout -`'s previous-branch when
`@{-1}` itself fails to resolve) must never wedge the agent's ability to run a command.

Known, deliberate scope limits (heuristic, not a sandbox — see worktree-only-writes for the
same tradeoff): a `cd other-repo && git checkout X` chain is judged against the ORIGINAL cwd's
enrollment unless the segment itself carries `git -C <dir>` (which IS honored); `git reset`/
`git rebase` (branch-tip mutation without a HEAD-ref change) are out of scope for v1 — the
incident this closes was a `checkout`, and scope-creeping into every ref-mutating command
risks false positives for a first cut. A bare `git checkout .` / `git checkout <path>` restore
(no branch involved) is excluded via the `--`/`.` checks in `_switch_target`, but an unusual
`git checkout <treeish> <path>` (no `--`, ambiguous even to git itself) can still be
misclassified as a branch switch — the escape hatch covers that rare case.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess  # noqa: S404 — fixed git argv, no shell
import sys
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

CONFIG_KEY = "worktree_only"  # SYNC: same knob as worktree-only-writes/worktree_only_writes.py
_FALLBACK_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT_S = 3.0

MESSAGE = (
    "BLOCKED — this is the repo's PRIMARY worktree; switching it to '{target}' risks colliding "
    "with other concurrent agents/sessions that share this checkout (this exact collision "
    "already happened once — Alex tg#6462/tg#6477). The primary worktree is for merge / pull / "
    "read-only only; checkout/switch there is blocked outside the default branch ({default}). "
    "Do the work in a separate worktree instead:\n"
    "    git worktree add ../wt-<feature> -b {target} origin/{default}\n"
    "    cd ../wt-<feature>   # then git checkout/switch there\n"
    "(worktree-only workflow, rig-provisioned; complements worktree-only-writes.) Deliberate "
    "one-off: set RIG_ALLOW_MAIN_EDIT=1."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"pin-primary-worktree: {msg}\n")


# ── git plumbing (SYNC: copied from worktree-only-writes/worktree_only_writes.py — each
# agent-hook is a standalone script by convention, no shared import between hook dirs) ──────

def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>`` and return trimmed stdout, or None on any failure."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            ["git", "-C", cwd or ".", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git {' '.join(args)} failed: {exc}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def default_branch(cwd: str) -> str:
    """The repo's default branch — repo-local detection, never hardcoded (SYNC, see the
    sibling worktree_only_writes.default_branch docstring for the full resolution-order
    rationale: origin/HEAD → repo-local init.defaultBranch → main/master existence → "main")."""
    ref = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[1] if "/" in ref else ref
    cfg = _git(cwd, "config", "--local", "--get", "init.defaultBranch")
    if cfg:
        return cfg
    for cand in ("main", "master"):
        if _git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}") is not None:
            return cand
    return _FALLBACK_DEFAULT_BRANCH


def _find_rig_yaml(cwd: str) -> Path | None:
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None). SYNC."""
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Minimal stdlib rig.yaml boolean reader. SYNC with worktree_only_writes /
    orchestrator_stays_thin — keep all three in lockstep if this parse ever changes."""
    in_block = False
    child_indent: int | None = None
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
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
            child_indent = indent
        if indent != child_indent:
            continue
        if head == key and ":" in line.strip():
            val = line.strip().split(":", 1)[1].strip().strip("\"'").lower()
            if val in ("true", "yes", "on", "1"):
                return True
            if val in ("false", "no", "off", "0"):
                return False
            return default
    return default


def worktree_only_enabled(cwd: str) -> bool:
    """Whether worktree-only enforcement is ON for this repo. env override > rig.yaml > OFF."""
    env = os.environ.get("RIG_WORKTREE_ONLY")
    if env is not None:
        return env.strip() == "1"
    root = _find_rig_yaml(cwd)
    if root is None:
        return False
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return False
    return _agent_hooks_bool(text, CONFIG_KEY, default=False)


def _escape_reason() -> str | None:
    if os.environ.get("RIG_ALLOW_MAIN_EDIT") == "1":
        return (os.environ.get("RIG_ALLOW_MAIN_EDIT_REASON") or "").strip() or "no reason given"
    return None


# ── primary-vs-linked worktree detection (the piece worktree-only-writes lacks) ─────────────

def _git_path(cwd: str, *rev_parse_args: str) -> Path | None:
    out = _git(cwd, "rev-parse", *rev_parse_args)
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        return p.resolve()
    except OSError:
        return None


def is_primary_worktree(cwd: str) -> bool | None:
    """True iff ``cwd`` sits in the repo's PRIMARY worktree (not a `git worktree add` linked one).

    git's own distinction: in the primary worktree ``--git-dir`` and ``--git-common-dir``
    resolve to the SAME directory (both the real ``.git``). In a linked worktree ``--git-dir``
    is ``<common>/.git/worktrees/<name>`` while ``--git-common-dir`` stays at the shared
    ``.git`` — they diverge. Returns None (undetermined) on any git/resolve failure so the
    caller can fail OPEN specifically on this signal.
    """
    gd = _git_path(cwd, "--git-dir")
    cd = _git_path(cwd, "--git-common-dir")
    if gd is None or cd is None:
        return None
    return gd == cd


# ── bash chain parsing (SYNC: chain-split copied from
# orchestrator-stays-thin/orchestrator_stays_thin.py._split_chain — same quote-aware algorithm) ─

def _split_chain(command: str) -> list[str]:
    """Split on shell operators (&&, ||, ;, |, newline, a bare control &) that lie OUTSIDE quotes."""
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
            segs.append("".join(buf))
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


# ── git checkout/switch classification ───────────────────────────────────────────────────────

_GIT_GLOBAL_OPT_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)


def _classify_git_segment(segment: str) -> tuple[str, list[str], str | None] | None:
    """If ``segment``'s head is ``git``, return (subcommand, rest_tokens, -C override), else None.

    Skips leading global options (so `git -C d checkout x` and `git -c k=v checkout x` still
    find the real subcommand); records a `-C <dir>` override so a cross-repo checkout is judged
    against THAT repo, not the shell cwd (mirrors worktree-only-writes' target-dir principle).
    """
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0].rsplit("/", 1)[-1] != "git":
        return None
    i = 1
    cwd_override: str | None = None
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        if opt == "-C" and i + 1 < len(toks):
            cwd_override = toks[i + 1]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):
            i += 1
    if i >= len(toks):
        return None
    return toks[i], toks[i + 1:], cwd_override


def _switch_target(subcommand: str, toks: list[str]) -> str | None:
    """The branch/ref a `checkout`/`switch` segment would move HEAD to, or None if it isn't a
    branch switch at all (a path-restore, a bare detach, or a `.`/no-arg form)."""
    if "--" in toks:
        return None  # `checkout <ref> -- <path>` / `checkout -- <path>` — restores a path only
    create_flags = {"-b", "-B"} if subcommand == "checkout" else {"-c", "-C"}
    positional: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in create_flags and i + 1 < len(toks):
            return toks[i + 1]  # explicit create-and-switch target wins outright
        if t == "-":
            positional.append(t)  # "previous branch" — a real target, NOT a flag despite the dash
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        positional.append(t)
        i += 1
    if not positional or positional[0] == ".":
        return None  # `checkout .` (bulk path-restore) / no target at all
    return positional[0]


def _resolve_effective_cwd(base_cwd: str, cwd_override: str | None) -> str:
    if not cwd_override:
        return base_cwd
    p = Path(cwd_override)
    if not p.is_absolute():
        p = Path(base_cwd or ".") / p
    return str(p)


def main() -> int:  # noqa: PLR0911 — a linear allow/allow/allow/block ladder reads clearer flat
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    for segment in _split_chain(command):
        classified = _classify_git_segment(segment)
        if classified is None:
            continue
        subcommand, rest, cwd_override = classified
        if subcommand not in ("checkout", "switch"):
            continue
        target = _switch_target(subcommand, rest)
        if target is None:
            continue

        eff_cwd = _resolve_effective_cwd(cwd, cwd_override)

        if target == "-":
            resolved = _git(eff_cwd, "rev-parse", "--abbrev-ref", "@{-1}")
            if not resolved:
                continue  # can't resolve "previous branch" → fail open on this segment
            target = resolved

        # Cheapest remaining gate: is this repo enrolled? (per the segment's OWN effective cwd,
        # so a cross-repo `git -C <other-repo> checkout` is judged by THAT repo's rig.yaml.)
        if not worktree_only_enabled(eff_cwd):
            continue

        default = default_branch(eff_cwd)
        if target == default:
            continue  # switching (back) to the default branch — always fine

        primary = is_primary_worktree(eff_cwd)
        if not primary:
            continue  # None (undetermined) or False (a linked worktree) → fail open / allowed

        reason = _escape_reason()
        if reason:
            warn(f"primary-worktree checkout allowed via RIG_ALLOW_MAIN_EDIT ({reason})")
            emit("allow", f"primary-worktree checkout allowed via escape hatch ({reason})")
            return 0

        emit("block", MESSAGE.format(target=target, default=default))
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
