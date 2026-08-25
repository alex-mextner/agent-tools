# No non-null assertion

## Avoid

Postfix `!` used to claim that a nullable value is present.

## Prefer

Check explicitly, establish presence by construction, use a discriminated state, or use optional chaining when absence is valid. Enforce with `typescript/no-non-null-assertion`.

## Why

`!` discards a fact the checker is deliberately preserving. If the assumption is wrong, the failure moves from compile time to runtime.
