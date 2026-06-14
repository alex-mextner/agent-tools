---
name: typed-errors-middleware
description: Use when handling errors in a web/API backend. Throw a typed application error and translate it to a response in one global error handler, instead of writing inline ad-hoc error responses at every call site.
---

# Typed errors + one global handler

Inline error responses — `return c.json({ error: "not found" }, 404)` scattered across
every route — drift apart over time: different shapes, different status codes for the
same condition, sensitive details leaking in some, missing in others. Centralize it.

## Pattern

- Define a small set of **typed application errors** carrying a status, a code, and a
  safe message:

  ```ts
  class AppError extends Error {
    constructor(readonly status: number, readonly code: string, message: string) {
      super(message);
    }
  }
  class NotFound extends AppError { constructor(what: string) { super(404, "not_found", `${what} not found`); } }
  class Forbidden extends AppError { constructor() { super(403, "forbidden", "Forbidden"); } }
  ```

- **Throw** them from handlers instead of building responses inline:

  ```ts
  const user = await users.find(id);
  if (!user) throw new NotFound("user");
  ```

- Translate them to responses in **one global error-handler middleware**, which also
  decides what's safe to expose and logs the rest:

  ```ts
  app.onError((err, c) => {
    if (err instanceof AppError) return c.json({ error: err.code }, err.status);
    log.error({ err }, "unhandled");           // full detail to logs, not to the client
    return c.json({ error: "internal_error" }, 500);
  });
  ```

## Why

One handler means one consistent error shape, one place that decides status codes, and
one place that ensures internal details (stack traces, DB messages) never leak to the
client. Inline responses can't give you any of that — they guarantee divergence and
accidental leaks. Throwing typed errors also lets the type system and tests reason about
failure modes. Pairs with `structured-logging-pino` (log `{ err }`, not `String(err)`)
and `no-silent-catch`.
