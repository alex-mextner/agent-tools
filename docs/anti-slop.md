# anti-slop integration

`anti-slop` is a specialized Oxlint plugin for patterns that erase TypeScript evidence or push uncertainty into ordinary application code. It is one layer of the broader [Mextner TypeScript policy](../skills/by-stack/frontend/ts/README.md).

## Source model

`agent-tools` pins the reviewed `alex-mextner/anti-slop` fork as the Git subrepo `vendor/anti-slop`. We do not consume anti-slop as a published/runtime dependency. Target repositories receive the supported vendoring payload from:

```text
vendor/anti-slop/skills/install-anti-slop/assets/anti-slop/
```

Rig copies that exact tree into `tools/oxlint/anti-slop/`. Updating anti-slop is an explicit reviewed subrepo-pointer change.

## Enforcement layers

1. **anti-slop** — specialized evidence-preservation AST rules.
2. **type-aware Oxlint** — general semantic TypeScript rules, including `typescript/no-unsafe-type-assertion` and `typescript/no-unnecessary-type-assertion`.
3. **Oxlint built-ins** — `typescript/no-non-null-assertion` and `typescript/ban-ts-comment` close the remaining escape hatches.
4. **TypeScript compiler** — `tsc --noEmit` remains the type-correctness gate.
5. **Oxfmt** — formatting. Biome is not part of the provisioned JS/TS baseline.

## Replacement of the old grep gate

| Existing check | Replacement | Why stronger |
| --- | --- | --- |
| `grep '\bas any\b|\bas unknown as\b'` | anti-slop + type-aware `no-unsafe-type-assertion` | AST/type relation, not spelling |
| `grep '@ts-ignore'` | `typescript/ban-ts-comment` | also bans `@ts-nocheck` and requires descriptions for `@ts-expect-error` |
| policy against postfix `!` | `typescript/no-non-null-assertion` | syntax-aware |
| widening then casting back | `anti-slop/no-widen-then-assert` | structural pattern |
| chained assertions | `anti-slop/no-chained-type-assertions` | structural laundering detection |
| redundant assertions | `typescript/no-unnecessary-type-assertion` | type-aware and fixable |
| unjustified necessary assertions | `anti-slop/require-safety-comment-for-type-assertion` | executable evidence requirement |

## Oxc config baseline

The target `oxlint.config.ts` registers `./tools/oxlint/anti-slop/index.ts` and renders Rig’s audited per-rule severities. Context-sensitive/API-shape rules remain off unless policy enables them; warning rules remain warnings. Type-aware mode also enables:

```ts
"typescript/no-unsafe-type-assertion": "error",
"typescript/no-unnecessary-type-assertion": "error",
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

## Rig target model

```yaml
linters:
  enabled: true
  bundles:
    anti-slop:
      source: vendor/anti-slop/skills/install-anti-slop/assets/anti-slop
      target: tools/oxlint/anti-slop
  items:
    oxlint:
      tool: oxlint
      role: linter
      path: oxlint.config.ts
      preset: mextner-ts
```

`source` is relative to `agent_tools_source`; `target` is repo-relative. An uninitialized/missing subrepo is an error, never a silent empty copy. Existing target content obeys Rig's normal `skip | overwrite | backup` policy.

The target repository declares compatible local `oxlint`, `oxfmt`, and `@oxlint/plugins` development dependencies. Bundle provisioning does not install global binaries or download code during `rig apply`.

For the complete overlap/addition/replacement matrix, see [TypeScript Policy vs. anti-slop](typescript-policy-vs-anti-slop.md).
