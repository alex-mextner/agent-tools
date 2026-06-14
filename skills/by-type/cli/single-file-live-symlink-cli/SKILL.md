---
name: single-file-live-symlink-cli
description: Use when a CLI is installed as a symlink to a checked-out repo, so the checked-out files ARE the running binary. Keep the main branch checked out (that's the deployed tool), and do all feature work in worktrees so you never edit the live binary in place.
---

# Single-file live-symlink CLI: main is the deployed binary

A common lightweight install for a personal CLI is a symlink from a PATH directory to a
file in a checked-out repo. The consequence is subtle but important: **the checked-out
working tree IS the running binary**. Whatever is on disk at the symlink target runs the
next time you invoke the command — including your half-finished edits.

## Rule

- **Keep `main` checked out at the symlinked path.** That checkout is, literally, the
  deployed tool. Checking out `main` = deploying; a dirty working tree = a half-deployed
  tool that runs your uncommitted changes every time you invoke it.
- **Do all feature work in a worktree**, not in the live checkout:

  ```bash
  git worktree add ../mycli-feature -b feature-x
  # edit, test, commit in ../mycli-feature — the live symlink target stays on clean main
  # when done and merged, the live checkout pulls main → the new version is "deployed"
  ```

- This keeps the running tool stable while you develop, and makes "release" a `git pull`
  on the main checkout rather than a build step.

## Why

Editing the live checkout means every invocation runs your work-in-progress — a broken
mid-edit state breaks the tool you're using *to do the editing*. Worktrees give you an
isolated place to develop while the symlinked main checkout stays clean and runnable.
"Checkout = deploy" is a feature here, not a bug — it just demands the discipline of never
developing in the deployed tree. Pairs with `universal/worktree-base-trap` and
`monorepo/worktree-isolation`.
