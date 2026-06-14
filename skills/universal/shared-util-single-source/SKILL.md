---
name: shared-util-single-source
description: Use when you notice you're about to write logic that already exists elsewhere, or when copy-pasting a snippet. Extract one shared utility instead of reimplementing inline; if duplication is truly unavoidable, mark both copies with a SYNC comment.
---

# Shared utility = single source of truth

When the same logic exists in two places, a fix applied to one and not the other is
a latent bug. Keep one source of truth.

## Rule

- Before writing logic, check whether it already exists. Reuse the existing utility,
  type, or component rather than reimplementing it inline.
- If the logic is duplicated, **extract it** into one shared function and call it
  from both sites.
- If duplication is genuinely unavoidable (e.g. the two copies live in packages that
  can't share a dependency, or a build boundary forbids the import), mark **both**
  copies with a `SYNC:` comment that points at the other, so the next editor knows
  to change them together:

  ```ts
  // SYNC: keep in step with src/server/format.ts:formatMoney — same rounding rules.
  function formatMoney(cents: number): string { ... }
  ```

## Why

A single source of truth means a fix or a behavior change happens once and is
correct everywhere. Silent duplication means it happens once and is *wrong*
everywhere else — and the divergence surfaces as a bug report months later, in the
copy nobody remembered.

## Caution

Two *similar* helpers are not automatically duplicates. One may be specialized for an
edge case the other doesn't handle. Read both fully before collapsing them — see
`dead-code-investigation`. Don't trade a real specialization for a false "DRY win".
