# Strict compiler

## Avoid

Disabling `strict`, weakening compiler options to make a local error disappear, or modeling values more broadly than reality.

## Prefer

Keep `strict: true`; also enable `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, and `noFallthroughCasesInSwitch` where applicable. Fix the model or control flow when the compiler objects.

## Why

The compiler is the foundation below every lint rule. Lint can reject suspicious syntax, but only the TypeScript checker can continuously prove assignability and control-flow facts across the program.
