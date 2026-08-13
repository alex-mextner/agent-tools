---
name: ts-strictness
description: Use when writing or configuring TypeScript — strict compiler flags, precise domain modeling, boundary parsing, inference preservation, and avoiding type escape hatches. Applies to any TypeScript frontend stack, not only React.
---

# TypeScript: keep the type checker honest

TypeScript only pays off if you let it fail. The target state is that illegal states are unrepresentable and runtime uncertainty is resolved at the boundary where it enters.

The canonical rule index is [`../README.md`](../README.md). Each rule there links to a dedicated guide with **Avoid / Prefer / Why**, matching the documentation structure used by the vendored anti-slop rules.

## Core rules

- **Strict compiler** — keep `strict: true`; add `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, and `noFallthroughCasesInSwitch`. [Guide](../rules/strict-compiler.md)
- **No `any`** — use the real domain type; `unknown` is a temporary boundary representation, not an application-layer contract. [Guide](../rules/no-any.md)
- **Parse at boundaries** — decode HTTP/JSON/config/forms/queues once and hand a named domain type inward. [Guide](../rules/parse-at-boundaries.md)
- **No unsafe assertions** — prove the type by control flow/schema; type-aware Oxlint rejects narrowing assertions, while anti-slop catches laundering patterns and requires `SAFETY:` evidence for the exceptional residue. [Guide](../rules/no-unsafe-assertions.md)
- **No unnecessary assertions** — remove assertions that add no type information. [Guide](../rules/no-unnecessary-assertions.md)
- **No postfix `!`** — establish presence explicitly or by construction. [Guide](../rules/no-non-null-assertion.md)
- **No TS suppression** — ban `@ts-ignore`/`@ts-nocheck`; narrowly justified `@ts-expect-error` only. [Guide](../rules/ban-ts-suppression.md)
- **Preserve inference** — prefer inference, `satisfies`, literal precision and owner contracts over widening then casting back. [Guide](../rules/preserve-inference.md)
- **Make illegal states unrepresentable** — discriminated unions and exhaustive handling over contradictory bags of optionals. [Guide](../rules/make-illegal-states-unrepresentable.md)
- **No reflection for ordinary typed code** — direct typed calls/property access and real dependency seams over reflection/module mocking. [Guide](../rules/no-reflection-for-typed-code.md)

## Oxc enforcement baseline

Use Oxc rather than Biome for this stack: Oxlint for linting and Oxfmt for formatting. TypeScript linting is type-aware.

At minimum, combine the complete vendored anti-slop rules with:

- `typescript/no-unsafe-type-assertion`
- `typescript/no-unnecessary-type-assertion`
- `typescript/no-non-null-assertion`
- `typescript/ban-ts-comment` configured to ban `@ts-ignore`/`@ts-nocheck` and require a description for `@ts-expect-error`

Keep `tsc --noEmit` as a separate compiler gate. Oxc linting complements the TypeScript compiler; it does not make the compiler optional.

## Why `unknown` is boundary-only

`unknown` is the correct static type for a value whose runtime shape is genuinely not yet known. The mistake is not receiving `unknown`; the mistake is preserving that uncertainty after the system has enough information to resolve it.

At an HTTP boundary, for example, `response.json()` is untrusted. Treat it as `unknown`, run `UserSchema.parse(raw)`, and from that point inward use `User`. If instead services return `unknown`, callers repeatedly write `typeof`/`in` checks and casts. Validation becomes duplicated, different callers infer different shapes, and the application no longer has one owner for the contract.

So the rule is **unknown at the uncertainty boundary → parse once → named type inward**, not “never use `unknown`.”
