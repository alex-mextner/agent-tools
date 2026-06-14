---
name: squash-merge-cleanup
description: Use after a pull request is merged, especially with squash-merge. Verify the PR is actually merged via the platform's merged state (a squashed branch never becomes an ancestor of main), then delete the branch and its worktree together. Also covers when to merge a fresh PR.
---

# Squash-merge cleanup discipline

With **squash-merge**, the feature branch's commits are collapsed into one new commit on
main — so the branch's tip **never becomes an ancestor of main**. This breaks the naive
"is it merged?" check: `git merge-base --is-ancestor branch main` returns false even
though the work *is* merged. Cleaning up based on that wrong check either deletes unmerged
work or refuses to delete merged work.

## Verify merge the right way

Don't ask git whether the branch is an ancestor. Ask the **platform** whether the PR is in
its merged state:

```bash
# Correct: the PR's recorded merged state, which squash-merge sets even though
# the branch tip isn't an ancestor of main.
gh pr list --state merged --head "$branch"        # appears here ⇒ truly merged
```

Once confirmed merged, delete the branch **and** its worktree **together** — leaving one
without the other is the usual source of stale clutter:

```bash
git worktree remove --force "$worktree"
git branch -D "$branch"
git push origin --delete "$branch"   # if it was pushed
```

## When to merge a fresh PR

- **Wait before merging** a just-opened PR — review and automated checks (CodeQL, CI, the
  review panel) have latency; merging immediately skips them. Give them time to run.
- **Never merge on red CI.** A failing check is a stop sign, not a suggestion.
- **Address every P1/P2 finding** before merge — don't merge over an unresolved
  high-severity review comment.

## Why

The squash-merge ancestry gotcha silently corrupts cleanup automation that trusts
`is-ancestor` — it's the single most common reason a "delete merged branches" script
either misses merged branches or threatens unmerged ones. Checking the platform's merged
state is the reliable signal. Deleting branch and worktree atomically keeps the repo from
accumulating dozens of stale trees. Pairs with `worktree-isolation` and
`universal/push-regularly`.
