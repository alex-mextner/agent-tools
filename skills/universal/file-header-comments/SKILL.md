---
name: file-header-comments
description: Use when creating or substantially editing a non-trivial source file. Add a short header block that records what the file is, how it's reached at runtime, the invariants it assumes, and past bugs — context grep can't reconstruct.
---

# File-header block comments for non-trivial files

A non-trivial file accumulates context that lives only in the author's head: how it's
actually reached at runtime, what it assumes about its callers, and which bugs have
already been fixed here. A short header captures that for the next reader.

## What to record

```ts
/**
 * @file  Resolves an editable element back to its source location.
 *
 * Accessed via: the inspector panel's "edit" action → this resolver →
 *               the write pipeline. (The user-facing path, so a reader knows
 *               WHEN this code runs without tracing every caller.)
 *
 * Assumptions: callers pass a stable element id; the source map is already
 *              loaded. (The architectural invariants — violating them is the
 *              usual cause of a bug here.)
 *
 * Past bugs: an off-by-one in the column index caused selection to jump one
 *            element; guarded by the exact-location check below. (So the next
 *            editor doesn't reintroduce a fixed bug.)
 */
```

The two load-bearing fields:

- **Accessed via** — the *user-facing path* that reaches this code. Lets a reader
  understand when it runs without reverse-engineering the call graph.
- **Assumptions** — the *architectural invariants* the code relies on. Most bugs in
  a file are a violated assumption; writing them down makes them checkable.

## Why

This is the cheapest place to leave the context that a `git blame` archaeology dig
would otherwise be needed to recover — and it sits right where the next editor will
look. Keep it short and current; a stale header is worse than none (see
`comment-hygiene`).
