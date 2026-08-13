# No unsafe type assertions

## Avoid

Narrowing a value with `as T` when the compiler has not established that `T` is true, especially `as any`, `as unknown as T`, and widen-then-cast-back flows.

## Prefer

Use control-flow proof, a parser/schema, a discriminated union, or a correctly typed owner API. Enable type-aware `typescript/no-unsafe-type-assertion` so narrowing assertions are rejected semantically, not by text matching.

If TypeScript cannot express an invariant that runtime code has genuinely checked, keep the assertion local and attach the specific `SAFETY:` invariant required by anti-slop.

## Why

An assertion is not validation. Type-aware Oxlint catches unsafe narrowing generally; anti-slop adds specialized structural patterns and requires evidence for the small residue of assertions that remain.
