# Oxc TypeScript output

These files are the reusable Oxc defaults supplied by `agent-tools`. Rig is the policy source: it resolves built-in defaults, the global Rig config, and repository `rig.yaml`, then provisions the effective Oxlint/Oxfmt files and any local rule providers required by the selected policy.

Target repositories need compatible local development dependencies:

```text
oxlint
oxfmt
@oxlint/plugins
```

## Configure policy in Rig

Do not copy an internal vendor path into project configuration and do not hand-maintain `oxlint.config.ts` as the source of truth. Configure rule groups and overrides in Rig:

```yaml
linters:
  rules:
    all: false
    groups:
      typescript-core: true
      anti-slop: true
    enable:
      - anti-slop/no-conditional-empty-object-spread
    disable:
      - anti-slop/no-module-mocking
    severity:
      anti-slop/no-reflect-get: error
```

The same block may live in the global Rig config to establish defaults for every repository. Repository `rig.yaml` refines those defaults. `all: true` enables every applicable discovered rule; `disable` and explicit `severity` entries can then narrow the result.

Rig generates `oxlint.config.ts`, vendors the pinned anti-slop implementation when an enabled rule needs it, and reconciles drift on subsequent applies. `.oxfmtrc.jsonc` remains the reusable formatter baseline. The checked-in `oxlint.config.ts` here documents the built-in TypeScript baseline and is also useful for testing the policy independently, but projects should express deviations through Rig rather than editing generated output.
