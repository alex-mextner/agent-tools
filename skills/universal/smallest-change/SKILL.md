---
name: smallest-change
description: Use when implementing a change in an existing codebase. Make the smallest change that solves the task. Don't refactor surrounding code "while you're in there", and never rewrite working code without explicit permission.
---

# Smallest reasonable change

The blast radius of a change is the surface a reviewer must verify and the surface
that can regress. Keep it small on purpose.

## Rules

- **Solve the task, nothing more.** Resist the urge to tidy unrelated code, rename
  things you happen to dislike, or "modernize" the file while you're in it. That's a
  separate commit at best, scope creep at worst, and it buries the actual change in
  noise.
- **Don't rewrite working code without explicit permission.** Code that works
  represents accumulated bug fixes and edge-case handling you can't see. Throwing it
  out to "do it properly" silently drops all of that. If you believe a rewrite is
  warranted, say so and get a yes first.
- **Match the surrounding style.** Consistency *within a file* beats your preferred
  external standard. A file that mixes two styles is harder to read than one that's
  uniformly in a style you wouldn't have picked.
- **Touch shared code only when the task requires it.** Changing a shared utility,
  store, or base component affects every consumer. If the feature needs it, do it
  deliberately and check the call sites; otherwise leave it alone.

## Why

Reviewers approve what they can verify. A 4-line diff that does one thing gets a
real review; a 400-line diff that does one thing plus "cleanup" gets a skim and a
rubber stamp — and the regression hides in the cleanup. Small, single-purpose
changes are also trivial to revert when they turn out wrong.

Pairs with `atomic-commits` (one logical change per commit) and
`dead-code-investigation` (don't delete things you don't understand).
