# PR-title lint — Conventional Commits

Validates the **PR title** against [Conventional Commits](https://www.conventionalcommits.org)
(`type(scope)!: subject`). With **squash-merge**, the PR title becomes the commit subject on
the default branch — so this keeps history clean and machine-parseable (changelog tools,
semantic-release, `git log` greps by type).

**Standard engine = [amannn/action-semantic-pull-request](https://github.com/amannn/action-semantic-pull-request)**
— the de-facto action for this.

## Quick start

```bash
cp ci/pr-title-lint/workflow.yml .github/workflows/pr-title-lint.yml
# Optional hard block: Settings -> Branches -> required checks -> add "pr-title-lint".
```

## Knobs

- `types` — the allowed Conventional types (default: the standard set). Trim to taste.
- `scopes` / `requireScope` — restrict scopes to a list, and/or require one. Leave `scopes`
  unset to allow any scope.
- `subjectPattern` — a regex the subject must match (default: lowercase start, no trailing
  period).

## Scope: title only

This gate checks the **PR title** (the squash subject). It does **not** validate every
commit message on the branch — that's a job for a `commit-msg` git-hook
([`../../git-hooks/`](../../git-hooks/)), which catches it locally at commit time. Run both
if you don't squash-merge.

## Pinning

`amannn/action-semantic-pull-request` is SHA-pinned (`# v6.1.1`).

## Why `pull_request_target`

So the action can post a helpful failure comment. It reads **only the title** from the event
payload — it never checks out or runs PR-head code, so the privileged token is safe.

## When to use

Any repo that squash-merges and wants a Conventional-Commit history (or feeds an automated
changelog / release tool). Pairs with the `atomic-commits` skill.
