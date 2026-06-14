---
name: no-type-escape-hatches
description: Use when writing typed code (TypeScript and similar). Don't reach for any, as any, as unknown as, @ts-ignore, or a loose Record to silence the type checker. Model the type correctly instead.
---

# No type escape hatches

A type checker is only useful if you don't lie to it. Every escape hatch trades a
compile-time error you'd fix in two minutes for a runtime bug you'll chase for two
hours.

## The hatches to avoid

- `any` — disables checking for the whole value and everything it touches.
- `as any`, `as unknown as Foo` — double-casts that launder one type into an
  unrelated one with zero verification.
- `@ts-ignore` / `@ts-expect-error` as a silencer (the latter is acceptable *only*
  with a comment explaining the expected error and a plan to remove it).
- `Record<string, unknown>` used as a lazy "I don't want to type this object".
- `JSON.parse(x) as T` / `response.json() as T` — asserts a shape that was never
  checked.

## What to do instead

- **Model the real type.** A discriminated union, a generic, a proper interface.
- **Validate at the boundary.** For external data (network, files, env), parse with
  a runtime validator (e.g. a zod/valibot codec) so the type is *earned*, not
  asserted. See `backend/zod-codec-parsing`.
- **If a dependency's types are wrong**, fix them at the source — patch the package,
  contribute a fix upstream — rather than casting around the bad type forever. See
  `library/patch-deps-not-cast`.

```ts
// BAD
const user = JSON.parse(raw) as User;          // unchecked
const x = (config as any).maybeMissing;         // silenced

// GOOD
const user = UserCodec.parse(JSON.parse(raw));  // validated → type is real
const x = config.maybeMissing;                   // typed as optional, handled
```

A grep-based pre-commit gate can catch the obvious hatches (`as any`,
`as unknown as`, bare `@ts-ignore`); see `git-hooks/`. The judgment cases —
"is this the *right* type" — stay with you.
