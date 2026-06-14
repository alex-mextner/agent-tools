---
name: atomic-commits
description: Use whenever you commit. One logical change per commit, committed often, with a conventional-commit message. Never batch unrelated changes into one commit.
---

# Atomic commits

Each commit should be one logical, self-contained change that builds and passes on
its own. This keeps history bisectable, review tractable, and revert surgical.

## Rules

- **One logical change per commit.** A bugfix and an unrelated rename are two
  commits, not one. If you can't describe the commit in a single clause without
  "and", it's probably two commits.
- **Commit often.** Don't sit on a large uncommitted working tree for an hour — a
  crash or a bad rebase then loses real work, and the eventual diff is unreviewable.
- **Each commit should be green** — it builds and its tests pass. A commit that only
  works "together with the next one" breaks `git bisect` and revert.
- **Conventional-commit message format** (see the `commit-msg` git-hook for
  enforcement):

  ```
  <type>(<optional scope>): <imperative summary>

  <optional body explaining WHY, not what>
  ```

  Types: `feat`, `fix`, `refactor`, `chore`, `test`, `docs`, `style`, `perf`.

## Examples

```
feat(auth): add refresh-token rotation
fix(parser): handle empty input without throwing
refactor: extract retry logic into withRetry helper
```

Not: `fix stuff and also update deps and rename a thing`.

The body answers "why was this needed", since the diff already shows "what
changed". Tie to an issue/ticket if your project uses one.
