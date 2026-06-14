---
name: comment-hygiene
description: Use when writing or editing code with comments. Comments should explain why, not narrate the diff. No temporal comments, no instructional comments, and never silently drop an existing comment.
---

# Comment hygiene

Comments are for the next reader, who has no memory of your edit session. Write them
for that reader, not for the diff.

## No temporal comments

A comment that describes the change *relative to a previous version* rots
immediately. Six months from now "new", "improved", "legacy", "updated", "old
approach" are meaningless — there is no "before" in the file, only the file.

```ts
// BAD
// New: now uses the faster path
// Legacy fallback (will remove later)
// Improved validation

// GOOD — describes what IS, and why.
// Fast path: callers in the hot loop skip the full parse when the cache is warm.
```

## No instructional comments left in code

`// TODO: actually implement this`, `// you should call init() first`,
`// remember to update the config` — these are notes-to-self, not documentation.
Either do the thing, encode the constraint in the type system / an assertion, or
file a tracked issue. Don't leave instructions floating in the source.

## Never silently drop a comment

When editing a block, don't quietly delete a comment that explains a non-obvious
invariant — that knowledge is often the only record of a past bug. Before
committing, scan your own diff for removed comments and make sure each deletion is
intentional:

```bash
git diff | grep -E '^-.*(//|#|/\*)'
```

## Why

Code says *what*; a good comment says *why* — the constraint, the gotcha, the
reason the obvious approach doesn't work. Everything else is noise that the next
reader has to mentally filter out.
