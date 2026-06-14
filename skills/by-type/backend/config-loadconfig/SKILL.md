---
name: config-loadconfig
description: Use when feature code needs a configuration value or environment variable. Read config through a single validated loader, never `process.env.X` scattered in feature code; validate at load and degrade gracefully when an optional value is absent.
---

# Config through one validated loader, not scattered process.env

`process.env.SOME_FLAG` sprinkled through feature code is unvalidated, untyped, and
invisible — you can't tell what the app needs without grepping the whole tree, a typo
in the variable name reads as `undefined` with no error, and tests can't inject values
cleanly.

## Rule

- Read environment **once**, at a single entry point, validate it, and expose a typed
  config object that the rest of the code consumes:

  ```ts
  // config.ts — the ONLY place that touches process.env.
  const Env = z.object({
    PORT: z.coerce.number().default(3000),
    DATABASE_URL: z.string().url(),
    FEATURE_X: z.coerce.boolean().default(false),
  });
  export function loadConfig() {
    const parsed = Env.safeParse(process.env);
    if (!parsed.success) throw new Error(`bad config: ${parsed.error}`);
    return parsed.data;
  }
  ```

- Feature code takes config **as a parameter / via DI**, not by reaching into a global
  or `process.env`. This makes it testable (inject a fake config) and explicit (the
  signature shows what it needs).
- **Validate at load**: a required var that's missing should fail loudly at startup, not
  read as `undefined` and explode three layers deep at request time.
- **Degrade gracefully for optional values**: if an optional integration's key is
  absent, disable that feature with a clear log line — don't crash the whole app over an
  optional dependency.

## Why

Centralizing config gives you one place to see everything the app needs, one place to
validate it, and a typed object instead of `string | undefined` everywhere. Scattered
`process.env` access is the opposite on every axis. An agent-hook
(`block-raw-process-env`) can flag raw `process.env.` in feature directories to keep the
discipline. Avoid the singleton anti-pattern (a module-load-time `const config = load()`)
where it complicates testing — prefer passing config in.
