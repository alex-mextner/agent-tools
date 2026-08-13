# Ban TypeScript suppression

## Avoid

`@ts-ignore`, `@ts-nocheck`, and unexplained `@ts-expect-error`.

## Prefer

Fix the type. When a compiler error is intentionally part of a test or an unavoidable external incompatibility, use the narrowest `@ts-expect-error` with a concrete description. Configure `typescript/ban-ts-comment` accordingly.

## Why

`@ts-ignore` silently survives after the underlying error disappears. `@ts-expect-error` is self-invalidating, and a required description preserves why the exception exists.
