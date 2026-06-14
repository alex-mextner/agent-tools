---
name: route-auth-inline
description: Use when adding authentication or authorization to web routes. Understand that path-prefix middleware only protects routes registered AFTER it, and that write handlers must use server-verified context, never values from the client request body.
---

# Route auth: order matters, and trust context not body

Two auth mistakes are easy to make and both are exploitable.

## 1. Middleware only protects routes registered after it

In most routers (Hono, Express, and similar), `app.use("/api/*", requireAuth)` applies
to routes registered *after* that line. A protected route declared *before* the
middleware — or on a path the glob doesn't actually cover — runs with no auth. The bug
is invisible: the route works, it just isn't guarded.

```ts
app.get("/api/admin/users", listUsers);   // BUG: registered BEFORE the guard → unguarded
app.use("/api/*", requireAuth);
app.get("/api/profile", getProfile);       // guarded
```

Register the auth middleware **before** the routes it must protect, verify the glob
actually matches them, and for anything sensitive consider an explicit per-route guard
rather than relying solely on prefix matching.

## 2. Authorize on server context, never on the request body

A write handler must decide *what the user may touch* from server-verified context —
the authenticated session, the membership the middleware looked up — **not** from
identifiers in the client-supplied body. A client can put any `orgId` / `ownerId` /
`role` in the body; trusting it is a membership/authorization bypass.

```ts
// BAD — client says which project; they can name one they don't own.
const project = await projects.find(body.projectId);

// GOOD — the auth middleware verified membership and stashed the project in context.
const project = c.get("checkedProject");
```

## Why

Both failures pass every happy-path test — the legitimate user's own requests work
fine. They only show up when an attacker registers a route-ordering gap or swaps an id
in the body. Putting the guard before the routes and authorizing off server-verified
context closes both. Pairs with `fail-closed-security`.
