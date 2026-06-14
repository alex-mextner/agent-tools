---
name: test-discipline
description: Use when writing, fixing, or reviewing tests. A trustworthy test exercises real production code, has pristine output, never tests a mock's own behavior, and is never deleted to make a suite go green.
---

# Test discipline: trustworthy tests only

A test suite is only worth running if its green means something. These rules keep
green honest.

## Never delete or neuter a failing test to get green

A failing test is a signal. Deleting it, commenting it out, or loosening its
assertion until it passes destroys the signal and hides the bug. Investigate the
root cause instead. **Changing the test to match the (wrong) code is a red flag**,
not a fix — the test encodes intended behavior; if reality disagrees, find out why.

## Tests must exercise production code

A test that re-implements the logic it's testing, or that asserts on a mock's
configured return value, proves nothing — it tests itself. Drive the *real* code
path and assert on its real output. Mock only at genuine boundaries (network, clock,
filesystem), never the unit under test.

```ts
// BAD — tests the mock, not the code.
const calc = { add: mock(() => 5) };
expect(calc.add(2, 3)).toBe(5);      // asserts the mock returns what you told it to

// GOOD — tests the real implementation.
expect(add(2, 3)).toBe(5);
```

## Pristine output

Warnings and errors printed during a test run are failures unless the test
explicitly asserts on them. Noise in the logs hides real regressions and trains
people to ignore output. If code under test is *supposed* to log an error, capture
and assert it; otherwise the run should be clean.

## Why

The cost of a bad test isn't zero — it's negative. It gives false confidence,
slows the suite, and breaks for the wrong reasons. Fewer honest tests beat many
that test mocks or tolerate noise.
