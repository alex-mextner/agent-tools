---
name: ts-strictness
description: Use when writing or configuring TypeScript — turning on strict compiler flags, modeling types precisely, and avoiding escape hatches (any / as / non-null !). Triggers on tsconfig strictness, a type error you're tempted to cast away, unions/discriminated unions, or "how do I type this". Applies to any TypeScript stack (lang-level), frontend or backend.
---

# TypeScript: keep the type checker honest

TypeScript only pays off if you let it fail. The goal is that an illegal state is
*unrepresentable*, not that the red squiggle is silenced.

## Turn on strict mode (and keep it on)

`"strict": true` in `tsconfig.json` is the baseline. Add the ones strict does not include:

- `noUncheckedIndexedAccess` — `arr[i]` is `T | undefined`, forcing you to handle the miss.
- `exactOptionalPropertyTypes` — `{ x?: number }` is not the same as `{ x: number | undefined }`.
- `noImplicitOverride`, `noFallthroughCasesInSwitch` — cheap correctness guards.

Never loosen strictness to make an error go away; fix the type.

## No escape hatches

- **`any`** erases checking and spreads silently through everything it touches. Reach for
  `unknown` and narrow, or model the real type.
- **`as` / `as unknown as`** asserts a lie the compiler then trusts. Use a type guard or a
  schema parse (`zod`) at the boundary instead of casting.
- **`!` (non-null assertion)** claims "trust me, not null". Prefer an explicit check or
  optional chaining; a wrong `!` is a runtime crash the checker could have caught.
- **`@ts-ignore` / `@ts-expect-error`** — only with a comment explaining why, and prefer
  `@ts-expect-error` (it fails if the error disappears, so it can't rot).

## Make illegal states unrepresentable

Model with discriminated unions, not a bag of optional booleans that can contradict:

```ts
// bad: isLoading && data can both be set; error && data ambiguous
type State = { isLoading: boolean; data?: User; error?: Error }

// good: exactly one variant is real at a time
type State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: User }
  | { status: 'error'; error: Error }
```

Switching on `status` gives exhaustiveness (with a `never` default) and no impossible combos.

## Parse, don't assume, at the boundary

Data from the network, `JSON.parse`, `process.env`, or a form is `unknown` in reality. Validate
it into a typed value with a schema (`zod`, `valibot`) at the edge; inside the app the type is
then *earned*, not asserted. A `response.json() as User` is a cast, not a check.

## Prefer `type` inference and narrow types

Let inference do the work for locals; annotate public function signatures and boundaries.
Use `readonly` and `as const` for data that shouldn't mutate, and literal unions
(`'sm' | 'md' | 'lg'`) over bare `string` where the set is known.
