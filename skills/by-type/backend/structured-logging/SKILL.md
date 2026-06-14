---
name: structured-logging
description: Use when logging in a backend service. Use a structured logger (pino or similar) and log errors as a nested error object so the stack trace is preserved — never String(error) or error.message, which throw the stack away.
---

# Structured logging; preserve the error object

Two logging mistakes throw away the information you'll need at 3am: unstructured string
logs, and stringifying errors.

## Use a structured logger

A structured logger (pino, or your stack's equivalent) emits JSON with consistent
fields, so logs are queryable (`level`, `requestId`, `userId`) instead of grep-only free
text. Attach context as fields, not by string-concatenating it into the message.

## Log the error object, not its string

```ts
// BAD — discards the stack trace; you get a message and nothing to locate it.
log.error(`failed: ${String(err)}`);
log.error({ error: err.message });

// GOOD — pass the Error under a key the logger knows to serialize (pino: `err`).
log.error({ err }, "payment capture failed");
```

`String(err)` and `err.message` capture only the message text. The **stack trace** —
the single most useful thing for finding where it happened — is lost. Passing the actual
`Error` object under the logger's error key (pino serializes `err` specially) preserves
the stack, the cause chain, and any custom fields on the error.

## Why

When something breaks in production, the log is often all you have. A structured log lets
you filter to the failing request; a preserved stack tells you the exact line. Strip
either away and you're left reconstructing the failure from a bare message — which is
exactly when you can least afford the archaeology. Pairs with `typed-errors-middleware`
(the global handler logs `{ err }` for everything it catches) and `no-silent-catch`.
