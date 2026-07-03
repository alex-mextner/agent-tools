#!/usr/bin/env python3
"""agents-hooks/v1 pre-write hook — enforce the worktree-only workflow.

On a PR-workflow repo the DEFAULT branch (main/master) is for merge / pull / read-only
operations ONLY: all file-authoring work belongs in a separate git worktree on a feature
branch. This gate DENIES an Edit/Write/MultiEdit/NotebookEdit while the checkout is sitting
on the repo's default branch, telling the agent to create/enter a worktree first. It is the
mid-session complement to the pre-push `protect-main` git-hook (agent-tools#157): that hook
stops a direct PUSH to main; this one stops the AUTHORING that precedes it, so the agent is
redirected to a worktree BEFORE it writes, not after it tries to push. (Alex tg#5742.)

Read-only tools and `git merge` / `git pull` are unaffected — they are not Edit/Write tool
calls, so this pre-write point never sees them.

PER-REPO, opt-in (this must NOT break a repo that legitimately works on main — e.g. 3d-cli):
the guard enforces ONLY where the repo opts in. Opt-in signal, first match wins:
  1. env  RIG_WORKTREE_ONLY=1 (force on) / =0 (force off)   — session/CI override + tests.
  2. the repo's committed rig.yaml → `agent_hooks.worktree_only: true`  (rig-provisioned).
  3. default OFF — a repo with no signal is never blocked.
So hyperide + the agent-ecosystem repos set `worktree_only: true` in their rig.yaml; 3d-cli
leaves it absent and is exempt automatically.

Escape hatch (the rare, deliberate main edit — mirrors block-raw-pr-merge / build guards):
  - env  RIG_ALLOW_MAIN_EDIT=1                 — allow this action despite being on main.
  - env  RIG_ALLOW_MAIN_EDIT_REASON='why'      — optional, logged when present.

Contract (agents-hooks/v1):
  stdin  : JSON event; target path in args.file_path/path/notebook_path; event.cwd is the shell dir.
           The verdict is keyed to the checkout the TARGET FILE lives in (its directory), not the
           shell cwd — they can differ (cwd in a worktree, an absolute write into the main checkout).
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": workflow discipline, not a security boundary — a git/parse failure must
never wedge the agent's ability to write. If the branch cannot be determined, we ALLOW.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 — a few read-only `git` queries are the whole job
import sys
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# The rig.yaml key (under agent_hooks) that opts a repo INTO worktree-only enforcement.
CONFIG_KEY = "worktree_only"
# Fallback default branch when neither origin/HEAD nor a local main/master branch is resolvable.
_FALLBACK_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT_S = 3.0

MESSAGE = (
    "You are on the default branch ({branch}) — the worktree-only workflow forbids authoring "
    "files here. main is for merge / pull / read-only operations only. Do the work in a "
    "separate worktree on a feature branch first, e.g.:\n"
    "    git worktree add ../wt-<feature> -b <feature> origin/{branch}\n"
    "    cd ../wt-<feature>   # then Edit/Write there\n"
    "(worktree-only workflow, rig-provisioned; Alex tg#5742.) Deliberate one-off main edit: "
    "set RIG_ALLOW_MAIN_EDIT=1."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"worktree-only-writes: {msg}\n")


def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>`` and return trimmed stdout, or None on any failure.

    None means "could not determine" → the caller fails OPEN (allows). We never raise: a repo
    that is not a git checkout, a missing git, or a timeout must not wedge writing.
    """
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


def current_branch(cwd: str) -> str | None:
    """The checked-out branch name, or None if detached/unknown (→ fail-open allow)."""
    name = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not name or name == "HEAD":  # "HEAD" = detached → cannot compare, allow
        return None
    return name


def default_branch(cwd: str) -> str:
    """The repo's default branch — detected repo-locally, never hardcoded and never machine-derived.

    Resolution order (each repo-local, so enforcement never swings with the developer's machine):
      1. ``git symbolic-ref refs/remotes/origin/HEAD`` (authoritative for any repo with a remote),
      2. a REPO-LOCAL ``init.defaultBranch`` (``git config --local`` — NOT the global setting, which
         would make enforcement machine-dependent (codex)),
      3. a local branch that exists — ``main`` preferred, then ``master``,
      4. the conventional ``main``.
    So a ``master``-only repo is judged against master. A repo whose default is neither and lacks
    origin/HEAD is a corner case (an enrolled PR-workflow repo has origin/HEAD) — set the repo-local
    ``init.defaultBranch`` to disambiguate.
    """
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
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None).

    Pure filesystem walk — no ``git`` subprocess — so the cheap opt-in check runs first and a
    non-enrolled repo never pays for a git query.
    """
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Read the boolean ``agent_hooks.<key>`` from rig.yaml text — deliberately minimal parse.

    Stdlib-only (the hook carries no PyYAML): scan for the top-level ``agent_hooks:`` block
    (indent 0) and read the key ONLY as a DIRECT child of it — a deeper-nested ``<key>:`` (e.g.
    under ``items.<hook>``) must NOT flip the guard repo-wide. A malformed / absent key returns
    the default (fail-open). This intentionally trades full-YAML fidelity for zero deps — rig.yaml
    files are rig-generated and well-formed, and a parse miss only means "don't enforce", which
    is the safe direction for a discipline gate. Kept in lockstep with the copy in
    orchestrator-stays-thin (SYNC: agent-hooks/orchestrator-stays-thin/orchestrator_stays_thin.py).
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


def worktree_only_enabled(cwd: str) -> bool:
    """Whether worktree-only enforcement is ON for this repo. env override > rig.yaml > OFF."""
    env = os.environ.get("RIG_WORKTREE_ONLY")
    if env is not None:
        return env.strip() == "1"
    root = _find_rig_yaml(cwd)
    if root is None:
        return False  # no rig.yaml → not enrolled → never block
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return False
    return _agent_hooks_bool(text, CONFIG_KEY, default=False)


def _escape_reason() -> str | None:
    """The escape-hatch reason if RIG_ALLOW_MAIN_EDIT=1 is set (reason optional), else None."""
    if os.environ.get("RIG_ALLOW_MAIN_EDIT") == "1":
        return (os.environ.get("RIG_ALLOW_MAIN_EDIT_REASON") or "").strip() or "no reason given"
    return None


def _target_dir(cwd: str, args: dict) -> str:
    """The directory whose branch decides the verdict: the WRITE TARGET's dir, else ``cwd``.

    The guard is about WHERE the file is authored, not the shell's cwd. A Write to an absolute path
    inside a default-branch checkout must be judged against THAT checkout's branch even when cwd is
    a feature worktree (and vice-versa) — so we resolve the branch from the target file's directory,
    falling back to cwd only when there is no path (codex). The file may not exist yet (a create) →
    walk up to the nearest existing ancestor so the git query has a real directory.
    """
    base = cwd or os.getcwd()
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


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    # Judge the checkout the TARGET FILE lives in, not the shell cwd (they can differ).
    where = _target_dir(cwd, args)

    # 1. Cheapest gate first: is this repo enrolled? (env / rig.yaml). No git cost if not.
    if not worktree_only_enabled(where):
        emit("allow")
        return 0

    # 2. Only enforce when actually sitting on the default branch.
    branch = current_branch(where)
    if branch is None:
        emit("allow")  # detached / not a git repo / git error → fail-open
        return 0
    if branch != default_branch(where):
        emit("allow")  # on a feature branch → exactly where authoring belongs
        return 0

    # 3. On the default branch: honor the deliberate-edit escape hatch, else BLOCK.
    reason = _escape_reason()
    if reason:
        warn(f"main edit allowed via RIG_ALLOW_MAIN_EDIT ({reason})")
        emit("allow", f"main edit allowed via escape hatch ({reason})")
        return 0

    emit("block", MESSAGE.format(branch=branch))
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
