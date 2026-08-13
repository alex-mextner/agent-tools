---
name: anti-slop
description: Use when a TypeScript/JavaScript repo adopts or hits anti-slop Oxlint rules. Explains the preferred replacement patterns, boundary-parsing policy, and how anti-slop composes with Oxlint's built-in TypeScript safety rules.
---

# anti-slop TypeScript policy

Use `anti-slop` as a narrow semantic layer on top of normal TypeScript strictness and Oxlint. It is not a formatter, a replacement for `tsc`, or a general-purpose lint preset.

## Preferred repairs

When a rule fires, preserve type evidence instead of suppressing the rule:

- keep inference rather than widening a known value;
- use `satisfies` when a value must be checked against a broader contract without replacing its inferred type;
- parse untrusted input once at the I/O boundary and pass named domain types inward;
- use discriminated unions or named owner contracts for dynamic domain state;
- replace module mocking with dependency seams that production code also understands;
- avoid reflection when ordinary typed calls/property access can express the operation;
- if a type assertion is genuinely required, document the exact checked invariant with a nearby `SAFETY:` comment.

Do not mechanically silence findings with `as`, `unknown`, `object`, broad dictionaries, `typeof` chains, or module mocks. Those are exactly the evidence-erasing patterns this policy is meant to expose.

## Complete escape-hatch baseline

Anti-slop deliberately focuses on its own opinionated rules. Pair it with Oxlint built-ins for the two TypeScript escape hatches that previously needed grep checks:

```ts
"typescript/no-non-null-assertion": "error",
"typescript/ban-ts-comment": [
  "error",
  {
    "ts-ignore": true,
    "ts-nocheck": true,
    "ts-expect-error": "allow-with-description"
  }
],
```

Keep `tsc --noEmit` (or the repo's equivalent typecheck) as a separate gate. Keep the repo's formatter/general lint rules as well; anti-slop is additive.

## Provisioning

For repos managed by Rig, prefer a vendored anti-slop bundle plus a Rig-managed Oxlint config instead of an npm dependency on the anti-slop repository. The upstream project is intentionally designed to be vendored and customized per team.

The target layout is:

```text
tools/oxlint/anti-slop/
  index.ts
  rules/
  shared/
oxlint.config.ts
```

`rig.yaml` should declare the anti-slop bundle and the Oxlint config; `rig apply` then owns convergence and `rig status` reports drift. See `docs/anti-slop.md` in agent-tools for the migration and replacement matrix.
