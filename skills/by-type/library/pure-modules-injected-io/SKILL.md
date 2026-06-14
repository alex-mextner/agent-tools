---
name: pure-modules-injected-io
description: Use when structuring a library or a testable module. Keep the logic pure and inject the I/O — pass subprocess spawns, fetches, file reads, and the clock in as parameters — so the logic is testable with fakes and no real side effects.
---

# Pure modules with injected I/O

A module that reaches out and *does* I/O directly — spawns a process, fetches a URL, reads
a file, reads the clock — can only be tested by actually performing that I/O (slow, flaky,
needs network/fixtures) or by monkey-patching globals (fragile, leaks between tests). Push
the I/O to the edges and inject it.

## Pattern

The logic is a pure function of its inputs *and its I/O dependencies*, which are passed in:

```ts
// I/O injected as deps → the function does no side effects of its own.
interface Deps {
  spawn: (cmd: string, args: string[]) => Promise<{ stdout: string; code: number }>;
  fetch: (url: string) => Promise<Response>;
  now:   () => number;
}

export async function syncRepo(name: string, deps: Deps) {
  const res = await deps.fetch(`/api/${name}`);   // injected, not global fetch
  ...
}
```

The real wiring (actual `spawn`, real `fetch`, `Date.now`) lives at the composition root.
Tests pass **fakes**:

```ts
const result = await syncRepo("x", {
  spawn: async () => ({ stdout: "ok", code: 0 }),   // no real process
  fetch: async () => fakeResponse({ ... }),         // no network
  now:   () => 1_700_000_000_000,                    // deterministic time
});
```

## Why

Injected I/O makes the logic **deterministic and fast to test** — no network, no
subprocess, no clock-dependent flakiness, no global monkey-patching that leaks across
tests. It also makes the dependencies *explicit*: the function signature lists exactly
what it touches, instead of hiding side effects inside the body. This is dependency
inversion at function granularity, and it pairs with `test-discipline` (tests exercise the
real logic, faking only the genuine boundaries) and `config-loadconfig` (config is one
more injected dependency).
