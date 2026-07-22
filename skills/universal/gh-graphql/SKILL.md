---
name: gh-graphql
description: Use when querying the GitHub GraphQL API from the shell — read PR/issue/repo data that the REST API cannot express in one call, paginate a connection, or script a GraphQL query. Prefer the `ghgql` wrapper (read-only by default) or the hook-safe inline `gh api graphql` recipe over ad-hoc, verbose, or merge-guard-tripping invocations.
---

# Querying the GitHub GraphQL API ergonomically (`ghgql`)

`gh api graphql` is the way to read data the REST API cannot return in one round-trip
(a PR's review threads, a repo's open PRs with nested fields, cross-object queries).
It is also verbose and easy to get wrong, and a few forms trip the `block-raw-pr-merge`
guard. This skill ships **`ghgql`**, a thin read-only-by-default wrapper, plus the
hook-safe recipes for calling `gh api graphql` directly.

`ghgql` lives next to this file. It is provisioned by `rig apply` (the skill directory is
symlinked into your agent's skills dir), so you can run it by its path or alias it:

```sh
alias ghgql="$HOME/.claude/skills/gh-graphql/ghgql"   # adjust for your harness skills dir
```

Keep the name **`ghgql`**: the `block-raw-pr-merge` guard maps a command whose name is
`ghgql` to the `gh api graphql` request it runs. Alias it to a *different* name and that
guard coverage is lost (the wrapper's own read-only refusal still applies, but the
authoritative merge gate keys on the `ghgql` name).

## Use `ghgql` for the common read

```sh
# A read-only query — just pass it, single-quoted:
ghgql 'query { viewer { login } }'

# With GraphQL variables (each -F name=value binds a $name in the query):
ghgql -F owner=cli -F name=cli \
  'query($owner:String! $name:String!){ repository(owner:$owner name:$name){
     pullRequests(first:20 states:OPEN){ nodes{ number title } } } }'

# Follow a connection to the end:
ghgql --paginate \
  'query($endCursor:String){ repository(owner:"cli" name:"cli"){
     issues(first:100 after:$endCursor){ nodes{ number } pageInfo{ hasNextPage endCursor } } } }'

# Post-process with jq, read the query from a file or stdin, or preview the argv:
ghgql --jq '.data.viewer.login' 'query { viewer { login } }'
ghgql -q @query.graphql
ghgql -q - < query.graphql
ghgql --dry-run 'query { viewer { login } }'      # print the gh command, run nothing
```

`ghgql` **refuses a mutation by default** (any `mutation`, `mergePullRequest`,
`enablePullRequestAutoMerge`, `enqueuePullRequest`, `mergeBranch`). Pass
`--allow-mutation` for a legitimate non-merge write. A **PR merge is never allowed
through `ghgql`**: the `block-raw-pr-merge` guard maps a `ghgql …` call to the `gh api
graphql` request it runs and blocks any merge mutation — even with `--allow-mutation`, or
a `@file`/stdin query it cannot read — so `ghgql` is never a raw-merge bypass. Merge a PR
with `gh ship`.

Any flag `ghgql` does not recognize, and everything after a lone `--`, is forwarded to
`gh api graphql` verbatim, so you never lose access to the underlying command.

## Calling `gh api graphql` directly — the hook-safe form

If you skip the wrapper, keep the query as a **single-quoted inline literal**. That form
is read by the merge guard and allowed:

```sh
gh api graphql -f query='query { repository(owner:"cli" name:"cli"){ id } }'
```

Forms the merge guard blocks (fail-closed — it cannot read the query text at pre-exec
time, so a merge mutation could be hiding in it):

- `gh api graphql -f query=@file.graphql` — file-backed query (unreadable).
- `gh api graphql --input body.json` — whole body from a file/stdin.
- `gh api graphql -f query="$(cat q.graphql)"` — query from a command substitution.
- `gh api graphql -f query="…$VAR…"` — a `$`/backtick that the shell expands at runtime
  inside a **double-quoted** value (the guard cannot know what it becomes).
- `gh api graphql -f $FIELD` / `gh api graphql $ARGS` — a field key or the whole arg set
  from a variable (could expand to a `query=<mutation>`).

To interpolate a value, don't splice it into the query string — pass it as a GraphQL
**variable** (`-F name=value`, referenced as `$name`). The variable's value may expand
(`-F owner=$OWNER` is fine); only the `query` field carries the operation.

A read-only query that merely **names** a merge mutation as a string literal — e.g.
`search(query: "mergePullRequest")` — is allowed: the guard strips GraphQL string
literals before deciding, so only an actual `mergePullRequest(` field call blocks.

## When NOT to use this

- Merging a PR → `gh ship <PR>` (green CI + required screenshots), never a raw
  `mergePullRequest` mutation.
- A plain REST read that one endpoint already answers → `gh api repos/{owner}/{repo}/…`.
