---
name: help-docs-sync
description: Use when adding or changing a CLI command's flags or behavior. Update the in-code --help/USAGE text and the command's docs page in the same commit, so the two never drift apart.
---

# Keep --help and the docs in sync, in the same commit

Two descriptions of a command — the `--help` output baked into the code, and the prose
docs page (`docs/commands/<name>.md`) — drift apart the moment one is updated without the
other. A user then reads docs that describe a flag that no longer exists, or `--help` that
omits a flag the docs mention. Both lose trust.

## Rule

When you change a command's flags, arguments, or behavior, update **both** in the **same
commit**:

- the in-code `--help` / `USAGE` string, and
- the command's docs page.

```
commit: feat(cli): add --json output to `report`
  - report.ts:    USAGE text now lists --json
  - docs/commands/report.md:  documents --json with an example
```

Better yet, **remove the chance of drift**: generate the docs from the help text (or
vice-versa), or add a test that asserts the two are consistent — every documented flag
appears in `--help` and vice-versa. Then "keep them in sync" is enforced, not remembered.

## Why

Documentation that contradicts the tool is worse than no documentation — it actively
misleads. The two sources drift because they're edited in different files at different
times; binding them to the same commit (or generating one from the other) makes the sync
structural instead of a thing you have to remember every time. Pairs with
`library/version-changelog-assert` — same "two things that must agree, enforce it"
shape.
