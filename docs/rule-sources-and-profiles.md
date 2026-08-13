# Rule sources and applicability profiles

Rig owns the effective development policy. Rule implementation repositories are inputs, not policy authorities.

## Pinned rule sources

| Source | Path | Role | Activation |
| --- | --- | --- | --- |
| `alex-mextner/anti-slop` | `vendor/anti-slop` | Reviewed generic structural Oxlint rules maintained in our fork. | Rig selects applicable rules and severity. |
| `typeonce-dev/ai-automation` | `vendor/ai-automation` | Pinned upstream reference/catalog for additional rule ideas and profile design. | No rules are enabled merely because the subrepo exists. |

Both are Git submodules pinned by the parent repository commit. Updating a source is an explicit gitlink change that must be reviewed like code.

`typeonce-dev/ai-automation` currently exposes no license metadata in its GitHub repository. The submodule therefore preserves provenance and lets reviewers inspect the exact upstream revision without copying its implementation into agent-tools. Until licensing is clarified, rules adopted from that catalog are independently implemented in our own providers or represented by equivalent existing Oxc/Rig policy.

## Applicability is part of policy

`all: true` means **all applicable rules**, not every rule known to every provider. A React rule is not applicable to a non-React repository; an Effect rule is not applicable merely because the project uses TypeScript.

Rig determines applicability from the declared stack and explicit repository policy. Framework-specific profiles are opt-in through stack evidence; repository overrides can further disable rules or change severity but should not silently manufacture a framework assumption that the declared stack contradicts.

The intended profiles are:

| Profile | Applicability | Default posture |
| --- | --- | --- |
| `typescript-core` | TypeScript | On with audited severities. |
| `anti-slop` | TypeScript/JavaScript where the local plugin is available | On with audited severities; opinionated API-shape rules stay off. |
| `architecture` | Repositories that declare layered/package-boundary conventions | Off until the repository declares the relevant boundaries/options. |
| `react` | React | Only rules that do not assume XState/Effect are candidates for automatic defaults; stronger culture rules require explicit profile choice. |
| `effect` | Effect | Applicable only when Effect is part of the declared stack. |
| `xstate` | XState | Applicable only when XState is part of the declared stack. |
| `next-tailwind` | Next.js + Tailwind | Applicable only when both assumptions hold or the repository explicitly selects the profile. |
| `review` | Changed-line review | Separate from full-repository lint; never silently promoted into a blocking full-tree rule. |

## ai-automation adoption map

The upstream catalog is useful because it separates rule behavior from applicability. We keep that distinction and do not treat its 49 custom Oxlint rules as one monolithic preset.

### Covered or replaced by existing Rig/anti-slop policy

| Upstream idea | Rig/agent-tools treatment |
| --- | --- |
| banned/type assertions | Covered by type-aware `typescript/no-unsafe-type-assertion`, `typescript/no-unnecessary-type-assertion`, anti-slop assertion rules, and the safety-comment rule. We do not add a duplicate blanket rule. |
| multiple function parameters | Independently implemented as `anti-slop/no-multiple-function-params`; off by default. |
| optional function parameters | Independently implemented as `anti-slop/no-optional-function-parameters`; off by default. |
| direct uncertainty/assertion laundering | Covered by anti-slop known-value widening, chained assertions, widen-then-assert, unsafe dictionary, unknown return/alias rules. |

### Architecture profile candidates

`no-api-backend-imports`, `no-api-repository-imports`, `no-reexport-only-modules`, and `no-sql-type-parameter` encode useful boundary policy, but their correctness depends on repository layout and ownership conventions. They belong in an `architecture` provider/profile with explicit path markers/allowlists rather than anti-slop.

`no-comments` is **not adopted as a general policy**. Rig deliberately requires focused comments for safety invariants, managed-file ownership, generated provenance, and non-obvious rationale. A blanket no-comments rule conflicts with that culture.

### React profile candidates

`no-react-component-inner-functions` and `no-react-non-component-function-exports` can be useful repository-culture rules but are not universal React correctness rules. `no-react-state-hooks` assumes an XState-centric architecture, so it must never become a default merely because React is detected.

### Effect profile candidates

Rules such as ambient nondeterminism, direct fetch/storage, swallowed errors, direct JSON, try/catch, switch/Match, Layer composition, service-option use, and Effect-specific inference rules are meaningful only in an Effect codebase. They form a separate `effect` profile and should be reviewed against current Effect APIs before independent implementation.

### XState profile candidates

Direct `createMachine`, selector-facade policy, multiple actor hooks, and single-use action/guard rules are `xstate` culture rules. Some additionally assume a repository-owned facade, so applicability may require configuration beyond dependency detection.

### Next/Tailwind profile candidates

CSS Modules, JSX `style`, fixed-height content, route-layout naming, arbitrary/restricted class policy, and design-token restrictions belong to `next-tailwind`. Most require project-owned theme/path options and therefore must not use global hard-coded assumptions.

### Review-only rules

Changed-line rules such as `no-let` or discouraging explicit implementation return types remain review-layer policy. They should not be silently converted into full-tree blocking rules because that would change both scope and migration cost.

## Selection contract

The effective order is:

1. built-in Rig defaults for applicable profiles;
2. global Rig config;
3. repository `rig.yaml`;
4. group/profile toggles and `all`;
5. per-rule `enable` / `disable`;
6. per-rule `severity`, which is final.

A generated linter config is merely the resolved output of that policy. It is marked as Rig-managed and is not a second source of truth.
