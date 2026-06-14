# ship — green-CI-gated PR merge + cleanup

`ship.sh <PR>` is a portable "merge this PR safely, then clean up" helper. It is **not** a
CI workflow — it's a local/CI command you run to merge a ready PR. Before it merges, it
runs the same preflights the CI gates in this directory enforce, so you can't merge a PR
that isn't actually ready even with a one-liner.

## What it refuses to merge

| Refusal | Why |
| ------- | --- |
| PR not OPEN / CONFLICTING / BEHIND base | Not mergeable / ruleset wants up-to-date branch. |
| Required status checks not all green | The green-CI gate (required-checks-only when branch protection + `jq` are present; else all checks). |
| Unresolved review threads | Same check as [`../review-threads/`](../review-threads/). |
| UI-touching PR with no screenshot | Same check as [`../screenshots/`](../screenshots/); override with `--no-screenshot-ok`. |
| Local branch has unpushed/diverged commits, or dirty worktree | Avoids merging stale/uncommitted local state. |

Then it squash-merges, deletes the remote branch, removes the local branch+worktree
(unless you're *inside* that worktree — then it's left so your session keeps a cwd), and
fast-forwards your main checkout.

## Quick start

```bash
cp ci/ship/ship.sh ~/bin/ship && chmod +x ~/bin/ship    # or wherever your PATH points
ship 123                                                # merge PR #123 when green
ship 123 --dry-run                                      # show what it would do
ship 123 --screenshot ./after.png "new dialog"          # attach + post visual proof
```

Wire it as a `gh` alias if you like:

```bash
gh alias set ship '!bash ~/bin/ship'    # then: gh ship 123
```

## All project coupling is OPTIONAL (env knobs)

Nothing org-/tracker-/layout-specific is hard-coded. Configure via env:

| Env | Default | Purpose |
| --- | ------- | ------- |
| `SHIP_DEFAULT_BRANCH` | `main` | Base branch. |
| `SHIP_MERGE_METHOD` | `squash` | `squash` / `merge` / `rebase`. |
| `SHIP_MAIN_CHECKOUT` | first worktree | Where to fast-forward after merge. |
| `SHIP_UI_PATH_REGEX` | common FE paths | What makes a PR "UI-touching". **Set empty to disable the screenshot gate.** |
| `SHIP_IMAGE_UPLOAD_CMD` | (unset) | Optional uploader for `--screenshot`: a command that takes the image (`{FILE}` token or `$1`) and prints a public URL. Without it, `--screenshot` just embeds a local-path note. |

## Flags

- `--skip-ci` — admin-merge bypassing the green-CI gate (use only when CI is billing-blocked
  or stuck; the other preflights still run).
- `--dry-run` — print, change nothing.
- `--no-screenshot-ok <reason>` — override the UI screenshot requirement, logged.
- `--screenshot <path> [desc]` — upload (via `SHIP_IMAGE_UPLOAD_CMD`) and post a screenshot
  as a PR comment; repeatable.

## What was stripped vs an internal version

This is generalized from a real in-house ship script. Removed/parameterized: any issue-
tracker coupling (ticket-id extraction, attaching proof to a tracker — replaced by the
generic `SHIP_IMAGE_UPLOAD_CMD` hook), hard-coded front-end path layout (now
`SHIP_UI_PATH_REGEX`), and any org/branch assumptions (now `SHIP_DEFAULT_BRANCH`). If you
want tracker integration back, set `SHIP_IMAGE_UPLOAD_CMD` to your tracker's attach command.

## When to use

When you merge PRs from the CLI and want the merge to be impossible unless the PR is green,
its threads resolved, and (for UI) its screenshot present — without trusting yourself to
remember. The CI workflows in this directory are the server-side backstop; `ship` is the
client-side gate.
