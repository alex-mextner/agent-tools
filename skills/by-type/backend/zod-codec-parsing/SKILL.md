---
name: zod-codec-parsing
description: Use when consuming data from outside your program — HTTP response bodies, JSON.parse output, message payloads, env. Validate it with a runtime schema/codec and safeParse, never cast the raw value to a type with `as T`.
---

# Parse external data with a codec, don't cast it

`JSON.parse(raw) as User` and `(await res.json()) as ApiResponse` are lies to the type
checker. The cast asserts a shape that was *never checked* — the value could be `null`,
missing fields, the wrong types, or an error payload, and the type system now believes
it's a valid `User`. The mismatch surfaces later as a confusing `undefined is not a
function` deep in unrelated code.

## Rule

Define a runtime schema (zod, valibot, or equivalent) and **parse** the external value
through it. The parse *earns* the type instead of asserting it:

```ts
const User = z.object({ id: z.string(), email: z.string().email(), age: z.number() });
type User = z.infer<typeof User>;

// BAD — unchecked assertion; type is fiction.
const user = (await res.json()) as User;

// GOOD — validated; the type is now true. safeParse to handle bad input gracefully.
const result = User.safeParse(await res.json());
if (!result.success) throw new AppError(502, "bad_upstream", "upstream returned an invalid shape");
const user = result.data;   // genuinely a User
```

Use `safeParse` (returns success/error) where you want to handle malformed input
gracefully; `parse` (throws) where malformed input is a genuine bug you want surfaced.

## Why

The boundary between your program and the outside world is exactly where types stop
being guaranteed — the compiler can't see what a remote server or a file will send. A
codec is the checkpoint that turns "I hope it's a User" into "it *is* a User or I found
out immediately, at the boundary, with a clear error". Casting skips the checkpoint and
moves the failure somewhere far from its cause. This is the boundary case of
`universal/no-type-escape-hatches`.
