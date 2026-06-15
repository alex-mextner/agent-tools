---
name: git-workflow
description: Use when working with git on destructive operations — naming a feature branch, undoing a commit, hard-resetting, or removing a worktree. Covers the safety rules the narrower git skills omit; branch naming, never hard-reset without checking status, fixup over reset, and worktree-removal safety.
---

# Git workflow safety

The git operations that lose work are the destructive ones — `reset --hard`,
`worktree remove`, a force-push — and they lose it quietly. These rules keep the
history honest and the working tree recoverable.

## Scope

This skill is the destructive-operation discipline. For the narrower concerns see:

- one-logical-change-per-commit, conventional messages → `atomic-commits`
- don't sit on local commits → `push-regularly`
- a fresh worktree bases on `origin/main`, not your HEAD → `worktree-base-trap`
- lint/typecheck/tests green before commit, no `--no-verify` → `pre-commit-gate`

## Rules

- **Branch naming follows a convention.** Use the project's prefix scheme so a branch
  maps back to its issue: `<TICKET>-<short-description>` (e.g. `HYP-123-add-logging`,
  `JIRA-456-fix-timeout`). A branch named `fix`, `wip`, or `test` is an orphan nobody
  can trace.
- **Never `git reset --hard` without first checking `git status`.** Uncommitted work a
  hard reset destroys is **not** recoverable from the reflog. Run `git status`; if
  anything is dirty, `git stash` or commit it first, *then* reset.
- **Prefer `git commit --fixup <commit>` over rewriting history with reset.** When you
  need to amend an earlier commit in a branch, a `--fixup` commit (later squashed with
  `rebase --autosquash`) is reversible and reviewable. A `git reset` to "redo" a commit
  discards the original and anything layered on top.
- **Never remove a worktree casually.** `git worktree remove` deletes the session's
  working directory — and deleting a *branch* is a separate operation. Remove a worktree
  only when explicitly asked to. To delete a branch that is checked out in a worktree,
  first switch that worktree off the branch (detach HEAD or check out another branch),
  or remove the worktree as part of a verified post-merge cleanup — never strand it.
- **Force-push only your own un-shared branch.** `--force-with-lease`, never bare
  `--force`, and never onto a shared or long-lived branch.

## Why

The reflog covers *committed* history, so a botched `reset --hard` on committed work is
recoverable for a while (reachable entries ~90 days, but commits orphaned by the reset
default to only ~30 days via `gc.reflogExpireUnreachable`). **Uncommitted** changes a
hard reset wipes are gone immediately, with no undo at all. The rule is: before any
destructive git command, make sure the thing it's about to delete is either already
committed or explicitly disposable.

```bash
# BAD — blind reset can vaporize uncommitted work
git reset --hard origin/main

# GOOD — verify first, preserve anything dirty
git status                 # anything uncommitted?
git stash                  # or commit it
git reset --hard origin/main
```
