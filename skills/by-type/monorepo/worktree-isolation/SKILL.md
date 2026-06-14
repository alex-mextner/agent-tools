---
name: worktree-isolation
description: Use when running multiple agents or parallel tasks against the same repo. Give each its own git worktree (and its own scratch/output dir) so they don't collide on the working tree, and clean up on exit — even on signals.
---

# Worktree isolation for parallel work

Two agents (or two parallel tasks) sharing one working tree will trample each other:
one's edits, branch switches, and uncommitted changes corrupt the other's view. Give each
its own **git worktree** so they have independent working trees over the same repo history.

## Pattern

```bash
# Each parallel run gets a uniquely-named, isolated worktree.
run_id="$(date +%s)-$$"
worktree="../.worktrees/agent-$run_id"
git worktree add "$worktree" -b "work/$run_id"

# ... do the work in $worktree — fully isolated from other runs ...

# Clean up on exit — INCLUDING on signal, so a killed run doesn't leak worktrees.
cleanup() { git worktree remove --force "$worktree" 2>/dev/null; }
trap cleanup EXIT INT TERM
```

Key points:

- **Unique name per run** (a run id), so concurrent runs never pick the same path.
- **Separate scratch/output dirs** too — if the tasks write build output or temp files,
  those must also be per-run, not a shared dir that two runs clobber.
- **Clean up on exit, including signals.** Register the cleanup on `EXIT`/`INT`/`TERM` so
  an interrupted or killed run still removes its worktree. Leaked worktrees accumulate and
  eventually confuse `git worktree list` and waste disk.

## Why

A worktree is the right isolation primitive: cheaper than a full clone (shared object
store, shared history) but with a fully independent working tree and HEAD — exactly what
parallel agents need so their edits and branch state don't interfere. The signal-safe
cleanup is the part people forget; without it, every crashed run leaves a stale worktree
behind. Pairs with `universal/worktree-base-trap` (verify each new worktree's base) and
`monorepo/squash-merge-cleanup` (tearing them down after merge).
