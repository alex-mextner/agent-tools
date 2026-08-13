# Preserve inference

## Avoid

Annotating a known value with a broader type and later asserting it back, widening literals prematurely, or replacing precise inference with `object`, `Record<string, unknown>`, or another generic container.

## Prefer

Let locals infer naturally. Use `satisfies` to validate against a contract without replacing the inferred type, and `as const`/readonly data where literal precision is intentional.

## Why

Inference is evidence. Once a value is widened, later code must rediscover or assert information the compiler already had. Anti-slop's `no-known-value-widening` and `no-widen-then-assert` make this policy enforceable.
