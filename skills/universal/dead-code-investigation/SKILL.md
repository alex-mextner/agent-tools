---
name: dead-code-investigation
description: Use before deleting any function, file, or module that "looks unused" or that a grep / static analyzer flags as dead. Investigate its history first — most apparently-dead code is a forgotten feature, a public API, or orphaned-by-migration, not safe to delete.
---

# Investigate before deleting "dead" code

A symbol that grep can't find a caller for is **not** automatically safe to delete.
A single-repo grep doesn't see external consumers, dynamic dispatch, reflection,
string-keyed lookups, or other worktrees. "Unused" almost always turns out to be
one of three things:

1. **A forgotten feature** — added, but never wired to its call sites, or the
   wiring broke during an adjacent migration. The concept is useful; it should be
   *fixed*, not deleted.
2. **A public API** — exported for an external consumer (another package, a script,
   a future use case). A grep over one repo is blind to it.
3. **Genuinely dead** — it was used, the call site was removed, and the utility was
   forgotten. This is the only case that warrants deletion.

## Investigation algorithm (mandatory before any deletion)

```bash
# 1. When was it added, when last touched, in which change?
git log --all --oneline -S '<symbol>'

# 2. Read the message of the commit that introduced it — what problem did it solve?
git show <commit>

# 3. Did a call site ever exist? When did it appear/disappear?
git log -S '<function>('

# 4. Check it wasn't orphaned by an adjacent refactor (moved/renamed/extracted
#    across files — a common "dead by migration" pattern).
```

Also check for non-grep references: dynamic dispatch, string-keyed registries,
public exports, config-driven loading, other repos/worktrees.

## Decision

After investigating, pick one explicitly — never act silently:

- **FIX** — the concept is useful but unwired/broken → reconnect it.
- **SALVAGE** — useful elsewhere → migrate it.
- **DELETE** — provably dead → remove it *and explain why in the commit message*.
- **ESCALATE** — unclear → ask, presenting the investigation findings, don't guess.

The same caution applies to "duplicates": two similar helpers don't mean one is
redundant. One may be specialized for an edge case and the other general. Read both
fully before collapsing them.
