---
name: push-regularly
description: Use at natural stopping points and before ending a work session. Push your commits regularly; never end a session with local commits that exist only on your machine.
---

# Push regularly; never sit on local commits

A commit that exists only in your local clone is one disk failure, one botched
rebase, or one forgotten worktree away from being lost — and it's invisible to
everyone else and to CI. Push at natural boundaries.

## Rule

- Push after each coherent unit of work lands and is green, not once at the very end.
- **Never end a session with unpushed commits.** Before you stop, check:

  ```bash
  git status -sb            # look for "ahead N" — those commits are local-only
  git log --oneline @{u}..  # exactly which commits aren't pushed
  ```

  If anything is ahead, push it (to a branch — see `monorepo/squash-merge-cleanup`
  for the branch-vs-main discipline).

## Caveat

"Push regularly" is about not *losing* work and keeping CI honest — it is not a
license to push directly to a protected/main branch. Push to a feature branch; open
a PR for review. The point is that your work lives somewhere durable and visible, not
only in a local working copy.

## Why

Local-only commits are work that doesn't exist as far as the team, the backup, and CI
are concerned. Pushing turns "I did it" into "it's there and verifiable", which is the
only version that survives a closed laptop.
