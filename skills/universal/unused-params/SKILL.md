---
name: unused-params
description: Use when a parameter, variable, or import becomes unused. Remove it entirely — the declaration and every call site — rather than silencing the linter with an underscore prefix.
---

# Remove unused params; don't underscore them away

When a parameter stops being used, an underscore prefix (`_unused`) just silences the
linter while leaving dead weight in the signature. The next caller still has to pass
something, the next reader still has to wonder what it's for, and the "unused" marker
slowly spreads as people copy the pattern.

## Rule

- If a parameter is genuinely unused, **remove it** — from the declaration *and* from
  every call site. A signature should list only what the function actually uses.
- Same for unused locals and imports: delete them, don't comment them out or prefix
  them.
- The underscore-prefix convention is for the rare case where an API *forces* a
  positional parameter you can't drop (a fixed callback signature, a destructured
  tuple where you need the second element). That's a constraint, not a license to
  leave dead parameters around.

```ts
// BAD — silenced but still dead; every caller still passes it.
function render(node: Node, _ctx: Context) { return node.html; }

// GOOD — gone from signature and call sites.
function render(node: Node) { return node.html; }
```

## Why

An unused parameter is a small lie in the interface: it claims the function needs
something it doesn't. Removing it shrinks the signature to the truth, simplifies every
call site, and prevents the underscore convention from metastasizing into "we always
just prefix the ones we don't use". Dead code is dead code whether or not the linter is
looking. (See `dead-code-investigation` before removing anything you're unsure about.)
