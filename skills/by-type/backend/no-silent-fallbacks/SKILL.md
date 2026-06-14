---
name: no-silent-fallbacks
description: Use when handling a possibly-missing value or a caught error in backend code. Don't paper over a missing required value with a `?? 0` / `?? ''` sentinel, and don't swallow a catch silently — guard and surface, or log why the swallow is safe.
---

# No silent fallbacks, no silent catches

Two habits hide bugs in plain sight: defaulting a *required* value to a sentinel, and
catching an error and doing nothing.

## No silent sentinel fallbacks for required values

`const total = data.total ?? 0` looks defensive but is a lie when `total` is *required*:
a missing `total` is a bug upstream, and `?? 0` converts that bug into a plausible-but-
wrong number that flows downstream and corrupts results silently.

```ts
// BAD — a missing required value becomes a wrong-but-valid 0.
const balance = account.balance ?? 0;

// GOOD — a missing required value is an error; guard and surface it.
if (account.balance == null) throw new AppError(500, "invariant", "account has no balance");
const balance = account.balance;
```

A `?? default` is only correct when the value is *genuinely optional* and the default is
the *intended* behavior — not as a reflexive null-guard on something that should always
be present.

## No silent catches

```ts
// BAD — the error vanished; a real failure looks like success.
try { await sync(); } catch {}

// GOOD — either handle it, or log WHY it's safe to ignore.
try {
  await sync();
} catch (err) {
  log.warn({ err }, "background sync failed; will retry next cycle");  // intentional, logged
}

// Fire-and-forget promises still need a .catch — an unhandled rejection is a silent failure.
void notify(user).catch((err) => log.error({ err }, "notify failed"));
```

If a catch truly should do nothing, leave a comment stating *why* it's safe — so the next
reader knows it's intentional, not forgotten.

## Why

Both patterns convert a loud failure into a silent wrong-answer, which is the most
expensive kind of bug: no error, no log, just incorrect data or a missing side effect
that someone notices weeks later. Guarding and surfacing keeps failures visible at the
point they occur. Pairs with `fail-closed-security` (the security-specific version) and
`structured-logging`.
