---
name: ts-strictness
description: Use when writing or configuring TypeScript — turning on strict compiler flags, modeling types precisely, and avoiding escape hatches (any / assertions / non-null !). Triggers on tsconfig strictness, a type error you're tempted to cast away, unions/discriminated unions, boundary parsing, or "how do I type this". Applies to any TypeScript frontend stack (lang-level at `frontend/ts`), not just React.
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

- **`any`** erases checking and spreads silently through everything it touches. At an actual
  untyped I/O boundary, keep the value `unknown` only long enough to run the expected parser or
  schema; do not pass `unknown` through ordinary application contracts and narrow it ad hoc later.
- **`as` / `as unknown as`** asserts a fact the compiler did not establish. Prefer inference,
  `satisfies`, a parser/schema at the boundary, or a domain-owned interface. If an assertion is
  genuinely necessary because TypeScript cannot express a checked invariant, document that exact
  invariant next to the assertion.
- **`!` (non-null assertion)** claims "trust me, not null". Prefer an explicit check, an invariant
  established by construction, or optional chaining; a wrong `!` is a runtime crash the checker
  could have caught.
- **`@ts-ignore` / `@ts-expect-error`** — never use `@ts-ignore`; use `@ts-expect-error` only when
  the error is intentional and document why. It fails if the suppressed error disappears, so it
  cannot silently rot.

For repositories using Oxlint, prefer AST rules for assertion/type-evidence policy over grep-based
checks. The `anti-slop` rule set covers chained assertions, known-value widening, widening followed
by assertion, unsafe broad dictionary contracts, and required `SAFETY:` comments for necessary
assertions. Keep separate enforcement for policies it does not cover, notably TypeScript directive
comments and non-null assertions.

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

## Parse, don't narrow, at the boundary

Data from the network, `JSON.parse`, environment/config input, message queues, or forms is
untrusted in reality. Keep the raw value at the boundary, validate it with the expected schema or
parser (`zod`, `valibot`, a generated decoder, or an owner-provided parser), and hand the parsed
domain type inward.

```ts
const payload: unknown = await response.json();
const user = UserSchema.parse(payload);
renderUser(user);
```

Do not replace parsing with a chain of `typeof`, property-existence checks, or casts spread across
business logic. A `response.json() as User` is a cast, not a check.

## Prefer inference and narrow types

Let inference do the work for locals; annotate public function signatures and real boundaries.
Use `satisfies` when you want to validate a value against a broader contract without replacing its
inferred type. Use `readonly` and `as const` for data that should not mutate, and literal unions
(`'sm' | 'md' | 'lg'`) over bare `string` where the set is known.
