---
name: fail-closed-security
description: Use when writing a security or access-control check, especially one that depends on an injected guard, a config flag, or an external lookup. Default to denial — an absent or errored check means "deny", never "allow".
---

# Security checks fail closed

A security decision has an asymmetric cost: wrongly denying access is an inconvenience;
wrongly granting it is a breach. So when a check can't reach a definite "yes" — the guard
wasn't injected, the lookup errored, the flag is undefined — the safe default is **deny**.

## Rule

```ts
// BAD — fails OPEN: a missing/undefined guard grants access.
function canEdit(ctx: { isOwner?: boolean }) {
  return ctx.isOwner ?? true;          // absent → allowed. Breach by omission.
}

// GOOD — fails CLOSED: anything but an explicit yes is a no.
function canEdit(ctx: { isOwner?: boolean }) {
  return ctx.isOwner === true;         // absent/undefined → denied.
}
```

- An **absent injected guard** defaults to `false` (deny), never `true`.
- An **errored permission lookup** denies (and logs), rather than treating the error as
  "probably fine, let them in".
- A **disabled / unconfigured** feature gate denies access to the gated thing, rather
  than leaving it wide open.
- `?? true` / `|| true` on a permission value is the canonical fail-open smell — see
  `no-silent-fallbacks`; for security the only acceptable default is the restrictive one.

## Why

Fail-open failures are invisible in testing: the authorized user is authorized, so
everything looks fine. The hole only appears for the *unauthorized* path — the exact
path you don't exercise on the happy day. Defaulting to deny means a bug in the check
produces a support ticket ("I can't access my thing"), not a silent breach. When in
doubt, the secure system says no. Pairs with `route-auth-inline` and `secret-handling`.
