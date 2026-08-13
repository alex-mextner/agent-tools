# Parse at boundaries

## Avoid

Letting `unknown` values from HTTP, JSON, environment/config, queues, storage, or forms propagate into business logic and repeatedly narrowing them with `typeof`, `in`, reflection, or casts.

## Prefer

Parse once at the I/O boundary with the owner-provided parser/schema/decoder, then expose a named domain type to the rest of the application.

```ts
const raw: unknown = await response.json();
const user = UserSchema.parse(raw);
renderUser(user);
```

## Why

A boundary parser converts runtime uncertainty into a checked contract at the point where uncertainty actually enters the system. Interior code can then remain simple and statically typed.

This does **not** mean `typeof` is universally bad. It is appropriate in a small boundary parser or when the runtime value genuinely has no stronger owner contract. Anti-slop's runtime-type rules are aimed at replacing scattered shape discovery with explicit contracts.
