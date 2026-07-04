# pin-primary-worktree

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Pins the repo's **PRIMARY worktree** to its default branch: BLOCKS a `git checkout` / `git
switch` that would move the primary checkout — never a `git worktree add`-created **linked**
worktree — onto anything other than the default branch (main/master). (Alex tg#6462/tg#6477.)

## The gap this closes

`worktree-only-writes` (the sibling `pre-write` gate) already denies an Edit/Write while the
checkout sits on the default branch. But it has two blind spots this hook covers:

1. It never sees a bare `git checkout <branch>` — that's a Bash call, not an Edit/Write tool
   call, so the pre-write point never fires on it.
2. Once the checkout HAS moved to a feature branch, `worktree-only-writes`' own logic treats
   "on a feature branch" as "exactly where authoring belongs" — true for a **linked** worktree,
   false for the **primary** one. Neither gate previously distinguished *which worktree* from
   *which branch is checked out*.

**The incident (2026-07-04):** an agent doing HYP-917 work ran `git checkout
feat/hyp-autofix-unsupported-framework` in the shared main checkout
(`/Users/ultra/work/hyperide`) instead of its own isolated worktree. No file damage happened
this time — the agent caught it and had authored everything in its real worktree — but a
second, concurrent agent then also checked out (and committed on) a different feature branch
in that same shared checkout before switching back to main. Any agent relying on the primary
checkout sitting on `main` can have it pulled out from under it mid-operation.

## Primary vs. linked worktree detection

Uses git's own distinction: in the primary worktree `git rev-parse --git-dir` and `--git-dir
--git-common-dir` resolve to the SAME directory (both the real `.git`). In a linked worktree
(`git worktree add`) `--git-dir` is `<common>/.git/worktrees/<name>` while `--git-common-dir`
stays at the shared `.git` — they diverge. Undetermined (git/resolve failure) fails OPEN.

## What's unaffected

- Checking OUT of a feature branch back to the default branch (the safe direction).
- `git worktree add` (a different worktree entirely — recommended alternative, shown in the
  block message).
- Any `git checkout`/`git switch` inside a LINKED worktree (exactly where branch work belongs).
- `git merge` / `git pull` / `git fetch` / `git worktree list` (not checkout/switch at all).

## Per-repo opt-in

Shares the **same** knob as `worktree-only-writes` — one feature, one flag:

1. `RIG_WORKTREE_ONLY=1` (force on) / `=0` (force off).
2. the repo's committed `rig.yaml` → `agent_hooks.worktree_only: true`.
3. default OFF.

## Escape hatch

```bash
RIG_ALLOW_MAIN_EDIT=1 RIG_ALLOW_MAIN_EDIT_REASON="deliberate, worktree overkill" \
  git checkout some-branch
```

## Known scope limits (heuristic, not a sandbox)

- A `cd other-repo && git checkout X` chain is judged against the ORIGINAL cwd's enrollment
  unless the segment itself carries `git -C <dir>` (which IS honored and re-resolves both the
  enrollment check and the default-branch/primary-worktree detection against that directory).
- `git reset` / `git rebase` (branch-tip mutation without a HEAD-ref change) are out of scope
  for v1 — the incident this closes was a `checkout`; scope-creeping into every ref-mutating
  command risks false positives for a first cut.
- `git checkout .` / a bare path restore is excluded; an unusual `git checkout <treeish>
  <path>` with no `--` (ambiguous even to git itself) can still be misclassified as a branch
  switch — the escape hatch covers that rare case.

## Test

```bash
python -m pytest -q tests/test_pin_primary_worktree.py
```
