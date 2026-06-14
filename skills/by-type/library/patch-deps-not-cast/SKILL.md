---
name: patch-deps-not-cast
description: Use when a dependency's published types are wrong or incomplete and the type checker complains. Fix the types at the source — patch the package locally, verify, and upstream the fix — rather than casting around the bad type everywhere it's used.
---

# Fix bad dependency types at the source, don't cast around them

When a library ships wrong or missing types, the tempting fix is `as any` / `as unknown
as TheRightType` at each call site. That scatters unchecked casts through your code,
each one a place the compiler is now lied to, and you repeat the workaround every time you
touch that API. The bad type is upstream — fix it upstream.

## Rule

1. **Patch the package's types locally** with a patch tool (`patch-package`, your package
   manager's `patches` field, or a local override / module-augmentation `.d.ts`). Now the
   correct type is applied once, at the boundary, and every call site is genuinely typed.
2. **Verify** the patch produces correct behavior, not just a quiet compiler.
3. **Upstream a PR** with the type fix so future versions don't need the patch — and you
   can drop your local override when it lands.

```ts
// BAD — repeated unchecked casts; the lie spreads with every use.
const x = (lib.thing() as any).value;
const y = lib.other() as unknown as RealType;

// GOOD — patch the dep's .d.ts / use module augmentation once; call sites are clean.
declare module "the-lib" {
  export function thing(): { value: string };   // the correct shape, applied centrally
}
const x = lib.thing().value;   // genuinely typed, no cast
```

## Why

A cast is a per-site, permanent lie that the next reader can't distinguish from a
legitimate one. A patched type is a single, centralized correction that the compiler then
enforces everywhere — and upstreaming it removes the problem for good (and for everyone).
The effort is front-loaded once instead of paid at every call site forever. This is the
dependency-level case of `universal/no-type-escape-hatches`.
