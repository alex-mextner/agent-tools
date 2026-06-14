---
name: idempotent-bootstrap
description: Use when a CLI does first-run setup — downloading assets, creating config, installing libraries. Make the bootstrap idempotent (safe to re-run) and non-fatal when offline, so it never blocks the core commands.
---

# First-run bootstrap: idempotent and non-fatal-if-offline

A CLI that does first-run setup (fetch libraries, scaffold a config dir, warm a cache)
must not punish the user for that convenience. Two properties keep it from becoming a
liability.

## Idempotent

Re-running the bootstrap — on the second invocation, after a partial failure, in CI — must
be safe and a no-op where already done:

```ts
async function bootstrap() {
  if (await exists(configDir)) return;        // already done → skip, don't redo
  await mkdir(configDir, { recursive: true }); // mkdir -p semantics: safe to repeat
  // fetch only what's missing, not everything every time.
}
```

Check-before-act, use create-if-not-exists semantics, fetch only what's absent. A
bootstrap that re-downloads everything on every run, or errors because the dir already
exists, fails one of these.

## Non-fatal when offline

The bootstrap is a *convenience*, not a precondition. If the network is down or an
optional asset can't be fetched, **warn and continue** — never block the core commands:

```ts
try {
  await fetchOptionalAssets();
} catch (err) {
  warn("could not fetch optional assets (offline?); core commands still work");
  // do NOT throw — the user can still run everything that doesn't need those assets.
}
```

## Why

First-run setup that crashes offline, or that re-does its work every run, turns a nicety
into friction — the user just wanted to run one command and now they're debugging the
installer. Idempotence makes re-runs free and partial failures recoverable;
non-fatal-if-offline keeps the optional from blocking the essential. Pairs with
`cli/structured-exit-codes` (a genuinely-missing *required* dep is still a clean 127 with
an install hint — the non-fatal rule is for *optional* bootstrap assets).
