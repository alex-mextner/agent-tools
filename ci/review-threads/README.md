# Merge gate: unresolved review threads

**Standard mechanism = GitHub branch protection "Require conversation resolution before
merging".** Prefer that native toggle if you can set it. This slot exists for two cases the
toggle doesn't cover:

1. You can't reach branch-protection admin (or want the gate to live *in the repo*, code-
   reviewed and versioned).
2. You want the same check inside a **ship script** preflight (see [`../ship/`](../ship/),
   which runs this exact GraphQL count before merging).

## What it does

Counts the PR's **unresolved** review threads via the GraphQL `reviewThreads` API and fails
if any remain. A "thread" is a review comment conversation; "resolved" is the green
*Resolved* state a reviewer/author toggles.

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `review-threads.sh` | **Any CI** + ship preflight | `gh api graphql` paginates threads, counts unresolved, exits non-zero if > 0. |
| `workflow.yml` | **GitHub Actions** | Runs the script as a PR check named `review-threads`. |

## Quick start

```bash
cp ci/review-threads/workflow.yml .github/workflows/review-threads.yml
# The workflow runs `bash ci/review-threads/review-threads.sh`, so the script must be
# present at that path in your repo — vendor the ci/ dir, or copy the script and adjust
# the `run:` path (e.g. to .github/scripts/):
cp ci/review-threads/review-threads.sh .github/scripts/review-threads.sh   # then edit the run: path
# Optional hard block: Settings -> Branches -> required checks -> add "review-threads".

# Standalone / ship preflight:
sh ci/review-threads/review-threads.sh 123
```

## Knobs

- `PR_NUMBER` (or `$1`) — the PR to check.
- `IGNORE_OUTDATED=1` — don't count threads GitHub marked *outdated* (default counts them;
  an unresolved thread is unresolved even if the line moved).

## Caveat — resolving a thread emits no webhook

Toggling *Resolved* does **not** fire a `pull_request` event, so the check won't auto-rerun
the instant you resolve the last thread. Push a commit, re-run the job, or rely on the
ship-time preflight ([`../ship/`](../ship/)) which always re-counts at merge time. The
workflow re-runs on `synchronize` (new commits) and `workflow_dispatch` (manual).

## Security note — tamper-resistant

A merge-BLOCKING gate must not run a script the PR can edit (a PR could weaken its own
gate). So the workflow uses `pull_request_target` and runs the **base-branch (trusted)**
copy of the script; it reads only the **PR number** from the event payload (data), never
checks out or executes PR-head code, and keeps a read-only token. The standalone
`review-threads.sh` is for local / ship-preflight use. `workflow_dispatch` takes a
`pr_number` input (a manual run has no PR in the event payload).

## When to use

Any repo where review comments must be addressed, not silently merged over — and you either
lack branch-protection admin or want the rule versioned in-repo. Pairs with the
[`../pr-checklist/`](../pr-checklist/) gate (unchecked task-list boxes).
