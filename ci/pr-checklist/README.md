# Merge gate: unchecked PR-body checkboxes

GitHub has **no built-in** "block merge until every PR-body checkbox is ticked". This slot
parses the PR body and **fails if any unchecked `- [ ]` remains** — so an acceptance-
criteria checklist in your PR template is actually enforced, not decorative.

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `checklist-gate.mjs` | parser (single source of truth) | `parseUnchecked(body)` returns the unchecked task-list lines. |
| `checklist-gate.test.mjs` | self-check | `node` (or `bun`) runs it; proves the parser without a framework. |
| `workflow.yml` | **GitHub Actions** | Imports the parser, fails the `PR Checklist` check on any unchecked box. |
| `pull_request_template.md` | PR template | Ships an "Acceptance criteria" task-list the gate enforces. |

## Quick start

```bash
cp ci/pr-checklist/workflow.yml          .github/workflows/pr-checklist.yml
cp ci/pr-checklist/checklist-gate.mjs    .github/scripts/checklist-gate.mjs
cp ci/pr-checklist/pull_request_template.md .github/pull_request_template.md   # optional
# REQUIRED to actually block (not optional — see "Enforcement" below):
#   Settings -> Branches -> required checks -> add "PR Checklist".
# Optional: run the parser test in CI:  node ci/pr-checklist/checklist-gate.test.mjs
```

The workflow imports the parser from `.github/scripts/checklist-gate.mjs` — keep both in
sync (one parser, two callers: the workflow and the test).

## Enforcement — a REQUIRED check, or it does not block the merge button

A `tier: block` workflow **only goes red** — by itself it does **not** block the merge
button. To actually ENFORCE this gate its `PR Checklist` context must be a **REQUIRED status
check** under **server-side branch protection**. rig-cli#5 provisions exactly that from the
`github:` block in `rig.yaml` — it lifts every `tier: block` gate into
`required_status_checks`. Without it, a GitHub-UI merge or a raw `gh pr merge` lands the PR
over a red check with boxes still unchecked — exactly how hyper-saas #543 merged through red
CI. See **[Client-side vs. server-side enforcement](../../README.md#client-side-vs-server-side-enforcement-the-543-gap)**
in the repo README.

## Why `pull_request_target` (and the hard safety rule)

The gate uses `pull_request_target` so the **base-branch** (trusted) copy of the workflow
and parser evaluate the PR — a PR that edits the workflow/parser can't weaken its own gate.
The non-negotiable rule that comes with this trigger: **never check out or run PR-head
code** under `pull_request_target` (the token is write-scoped). This workflow doesn't — it
checks out the default (base) ref and reads only the PR **body** from the event payload,
never interpolated into a shell. `permissions: contents: read` trims the token as well.

**Caveat:** because it runs from base, the gate does **not** run on the PR that first adds
it (base has no gate yet). It goes live from the next PR.

## Knob — what counts as a checkbox

`parseUnchecked` treats a Markdown bullet (`-`, `*`, `+`) followed by ` [ ]` as an
unchecked task-list item, with optional indentation. Inline `[ ]` that isn't a bullet is
ignored. Edit `UNCHECKED_RE` in `checklist-gate.mjs` to scope it (e.g. only under a specific
heading) — and update the test.

## When to use

Any repo with a structured PR template whose acceptance criteria must be satisfied before
merge. Pairs with [`../review-threads/`](../review-threads/) (unresolved comments) and
[`../screenshots/`](../screenshots/) (mandatory images).
