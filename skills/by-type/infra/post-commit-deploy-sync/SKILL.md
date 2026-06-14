---
name: post-commit-deploy-sync
description: Use when a repo's committed files are the source of truth for a downstream system (a config-driven appliance, an IoT controller, a generated artifact). A post-commit hook can auto-sync the change downstream — but keep it idempotent, fast, and non-blocking.
---

# Post-commit sync for config-as-source-of-truth repos

When a repo *is* the configuration for some downstream system — a home-automation
controller, an embedded device's firmware config, a generated-artifact pipeline — a
`post-commit` git hook can push the committed change to that system automatically, so
"commit" and "deploy" are one step.

## Pattern

```bash
# .git/hooks/post-commit  (or a lefthook post-commit) — runs AFTER the commit succeeds.
#!/bin/sh
# Sync the just-committed config to the downstream system.
# Idempotent: re-running re-syncs the same state, no harm.
sync-to-target --from "$(git rev-parse --show-toplevel)" || {
  echo "post-commit: sync failed (commit still succeeded); re-run sync-to-target manually" >&2
  exit 0   # do NOT fail — the commit already happened; blocking here helps no one.
}
```

Keep it:

- **Idempotent** — syncing the same committed state twice is safe (the downstream system
  ends up in the same place). Re-running after a transient failure just works.
- **Non-blocking on failure** — `post-commit` runs *after* the commit is already made;
  exiting non-zero can't un-commit it and only produces a confusing error. A failed sync
  should warn and tell the user how to re-sync, not pretend the commit failed.
- **Fast / async** — don't make every commit wait on a slow network deploy; kick off the
  sync and return, or sync only the delta.

## Why

This collapses a two-step ritual (commit, then remember to deploy) into one, which is
exactly right when the repo is the canonical config — there's no meaningful gap between
"committed" and "should be live". The idempotent + non-blocking constraints keep the
convenience from becoming a hazard: a flaky downstream never blocks your commits, and a
double-run never corrupts the target. Contrast with `git-hooks/` *pre*-commit gates, which
*should* block (they run before the commit and guard its quality).
