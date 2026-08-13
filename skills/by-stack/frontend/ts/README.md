# TypeScript policy

This directory defines the reusable TypeScript policy supplied by `agent-tools`. Rig is the configuration and provisioning layer: it resolves global + repository policy, selects applicable rule groups from the declared stack, and renders the concrete compiler/linter/formatter configuration.

The policy is intentionally broader than `anti-slop`. TypeScript compiler strictness establishes the static baseline, Oxlint provides syntax- and type-aware enforcement, anti-slop contributes focused evidence-preservation rules, and Oxfmt owns formatting.

## Core rules

| Rule | Prefer | Enforcement |
| --- | --- | --- |
| [strict-compiler](rules/strict-compiler.md) | keep strictness on; model the real type | `tsc` / tsconfig |
| [no-any](rules/no-any.md) | concrete domain types; `unknown` only at an untyped boundary | Oxlint + compiler |
| [parse-at-boundaries](rules/parse-at-boundaries.md) | parse/decode I/O once, then pass domain types inward | policy + anti-slop |
| [no-unsafe-assertions](rules/no-unsafe-assertions.md) | prove by control flow/schema; use `SAFETY:` only for an unexpressible checked invariant | type-aware Oxlint + anti-slop |
| [no-unnecessary-assertions](rules/no-unnecessary-assertions.md) | remove assertions that add no information | type-aware Oxlint |
| [no-non-null-assertion](rules/no-non-null-assertion.md) | explicit check or construction invariant | Oxlint |
| [ban-ts-suppression](rules/ban-ts-suppression.md) | fix the type; narrowly justified `@ts-expect-error` only | Oxlint |
| [preserve-inference](rules/preserve-inference.md) | inference, `satisfies`, `as const`; do not widen then cast back | Oxlint + anti-slop |
| [make-illegal-states-unrepresentable](rules/make-illegal-states-unrepresentable.md) | discriminated unions and owner contracts | compiler/policy |
| [no-reflection-for-typed-code](rules/no-reflection-for-typed-code.md) | direct calls/property access and explicit dependency seams | policy + anti-slop |

## anti-slop provider

The pinned fork at `vendor/anti-slop` is the implementation/source-of-truth for anti-slop rules and their per-rule guides. Vendoring it does not activate every rule. Rig applies the audited baseline and any global/repository overrides.

Default anti-slop severities are:

- **error:** chained assertions, known-value widening, widen-then-assert, unsafe dictionary contracts, assertion `SAFETY:` evidence, broad object parameters, unknown aliases, and unknown returns;
- **warn:** `Reflect.get`, `Reflect.apply`, module mocking, and unknown parameters;
- **off:** runtime `typeof`, conditional empty-object spread, and `shape` in symbol names.

See [the complete grouped policy table](../../../../docs/typescript-policy-vs-anti-slop.md) for every rule, default, enforcement mechanism, guide link, and the Rig override model.

## Oxc baseline

Use Oxc as the frontend toolchain: **Oxlint** for linting and **Oxfmt** for formatting. Type-aware linting is part of the TypeScript baseline. `typescript/no-unsafe-type-assertion`, `typescript/no-unnecessary-type-assertion`, `typescript/no-non-null-assertion`, and `typescript/ban-ts-comment` remain enabled independently of anti-slop because they enforce different semantic or language-level invariants.

`oxlint.config.ts` is a generated/provisioned artifact. Do not make it the policy source: select rules and severity through Rig configuration so the same policy can be inherited globally, overridden per repository, inspected, diffed, and reapplied deterministically.
