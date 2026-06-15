---
name: worktree-base-trap
description: Use when creating a fresh git worktree or branch for feature work, especially when your work is stacked on top of branches not yet merged to main. Verify the new worktree's base before you start coding — a fresh worktree usually bases on origin/main, not your current HEAD.
---

# The worktree-base trap

Most "new worktree" / "new branch" helpers base the new tree on `origin/<default
branch>` (a fresh starting point), **not** on your current HEAD. If you're working in
a stack — feature B branched off feature A which isn't merged yet — a fresh worktree
silently gives you the *old* code without A's changes, and you discover it only after
writing code against a base that's missing what you depend on.

## Rule

Immediately after creating a worktree or branch, **verify the base** before writing
any code:

```bash
git log --oneline -1            # is this the commit you expect to be on top of?
ls path/to/expected/new/dir     # does a directory/file from the stack exist?
```

If the base is wrong and you have **not yet made any commits** on the new branch,
re-seat it onto the top of your stack — but check `git status` first; a hard reset
destroys uncommitted changes irrecoverably (see `git-workflow`):

```bash
git status                      # clean? (a hard reset wipes anything dirty)
git reset --hard <top-of-stack-ref>
```

(Only while the branch has no commits of its own — otherwise you'd discard them.)

## Why

The failure is silent: the tooling did exactly what it was designed to do, and the
code compiles, so nothing screams. You only notice when a symbol from the unmerged
branch is missing — often deep into the work, after you've built on the wrong
foundation. A two-command check at creation time costs nothing and removes a whole
class of "why is this not here" confusion.

Pairs with `monorepo/worktree-isolation` (parallel-agent isolation) and
`monorepo/squash-merge-cleanup` (tearing the worktree down again).
