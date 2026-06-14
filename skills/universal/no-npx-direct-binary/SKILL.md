---
name: no-npx-direct-binary
description: Use when invoking a project-local CLI tool in scripts, hooks, or commands. Call the installed binary directly (or via your package manager's runner) instead of npx, and don't hardcode absolute paths to PATH-resolved tools.
---

# Use the installed binary, not npx

`npx <tool>` re-resolves the tool every invocation: it may hit the network, may pull
a *different* version than the one your project pins, and adds startup latency to
every call. In a hook or a hot script that runs constantly, that's both slow and
non-deterministic.

## Rule

- Call the **installed** binary directly — the one in `node_modules/.bin/` (or your
  package manager's equivalent), which is the exact pinned version:

  ```bash
  # BAD — re-resolves, may fetch, may drift version.
  npx tsc --noEmit
  npx biome check .

  # GOOD — the pinned local binary.
  node_modules/.bin/tsc --noEmit
  node_modules/.bin/biome check .
  ```

- If you use a runner, use your package manager's own (`bunx`, `pnpm exec`,
  `yarn <bin>`) which respects the lockfile, rather than reaching to the network.
- **Don't hardcode absolute paths** to tools that live on `PATH` (`/usr/local/bin/…`,
  a home-dir path). Absolute paths break on every other machine and in CI. Let `PATH`
  resolve them, or use the project-local binary.

## Why

Determinism and speed. The version you tested with should be the version that runs —
not whatever `npx` decides to fetch today. And a script that hardcodes a machine-local
path works on exactly one machine; a script that uses the project-local binary works
everywhere the repo is checked out.
