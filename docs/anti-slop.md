# anti-slop integration

`anti-slop` is a specialized Oxlint plugin for rejecting TypeScript/JavaScript patterns that erase type evidence or push uncertainty into ordinary application code. It complements, rather than replaces, TypeScript strictness, `tsc`, and general lint/formatting.

## Enforcement layers

Use one AST-based lint surface instead of overlapping regex guards:

1. **anti-slop plugin** — opinionated evidence-preservation rules.
2. **Oxlint TypeScript built-ins** — cover remaining escape-hatch policy:
   - `typescript/no-non-null-assertion`: forbid postfix `!` assertions.
   - `typescript/ban-ts-comment`: forbid `@ts-ignore` and `@ts-nocheck`; permit `@ts-expect-error` only with a description.
3. **TypeScript compiler** — keep `tsc --noEmit` (or the repo's equivalent) as the type correctness gate.
4. **General linter/formatter** — keep Biome/Oxlint/general style rules as appropriate; anti-slop is not a formatter.

## What replaces the old `no-type-escape-hatches` grep

| Existing check | Replacement | Notes |
| --- | --- | --- |
| `grep '\bas any\b|\bas unknown as\b'` | anti-slop assertion/evidence rules | AST-aware; catches more than the literal spelling. |
| `grep '@ts-ignore'` | `typescript/ban-ts-comment` | Can also ban `@ts-nocheck` and require descriptions on `@ts-expect-error`. |
| policy against postfix `!` | `typescript/no-non-null-assertion` | No custom regex needed. |
| widening then casting back | `anti-slop/no-widen-then-assert` | Not reliably expressible with grep. |
| chained assertions | `anti-slop/no-chained-type-assertions` | Covers multi-step assertion laundering. |
| unjustified necessary assertions | `anti-slop/require-safety-comment-for-type-assertion` | Requires a local invariant explanation. |

Do **not** remove `tsc` or the normal lint/format gate when enabling anti-slop.

## Recommended policy config

The anti-slop plugin registration is expected at `./tools/oxlint/anti-slop/index.ts`. Alongside all anti-slop rules, enable:

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

The exact anti-slop rule list should come from the vendored upstream bundle, not be reconstructed from memory.

## Rig target model

The intended committed repo configuration is terse:

```yaml
linters:
  enabled: true
  bundles:
    anti-slop:
      source: linters/anti-slop
      target: tools/oxlint/anti-slop
  items:
    oxlint:
      tool: oxlint
      role: linter
      path: oxlint.config.ts
      preset: anti-slop
```

`source` is relative to the resolved `agent_tools_source`; `target` is repo-relative. `rig apply` should copy/reconcile the bundle and render the preset config, while `rig status` should report drift in either surface. This avoids embedding a directory of TypeScript source into YAML and keeps anti-slop vendored/versioned with the agent-tools checkout.

### Conflict semantics

A pre-existing `tools/oxlint/anti-slop/` or `oxlint.config.ts` must honor Rig's normal `defaults.on_conflict` policy. Rig must never silently overwrite a project-customized anti-slop fork. Migration should review the diff first; after Rig takes ownership, drift becomes explicit.

### Dependencies

The target repository still needs compatible local dev dependencies for `oxlint` and `@oxlint/plugins`. Dependency mutation should remain package-manager-aware rather than being hidden inside a file-copy action. Repositories can either declare these dependencies themselves or use a future package-dependency provisioning surface; the linter bundle must not install a global Oxlint binary.

## Policy alignment for agents

When fixing findings, prefer:

- inference over explicit widening;
- `satisfies` over replacing an inferred type with a broad annotation;
- parsing/decoding at the I/O boundary, with named domain types inward;
- discriminated unions and named owner contracts over broad dictionaries or `object`;
- explicit dependency seams over module mocking;
- direct typed calls/property access over reflection;
- removing assertions where possible, or a specific `SAFETY:` invariant when an assertion is genuinely necessary.

Do not “fix” anti-slop by laundering the same uncertainty through another top type or assertion.
