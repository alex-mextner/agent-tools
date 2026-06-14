---
name: tdd-red-first
description: Use when implementing any feature or bugfix, before writing implementation code. Write a failing test first, confirm it fails for the right reason, then write the minimal code to make it pass.
---

# TDD: red first, for the right reason

Test-driven development is not "write tests after". It is a tight loop that forces
you to specify behavior before implementing it, and proves the test actually
exercises the thing it claims to.

## The loop

1. **Red** — write one failing test for the next small piece of behavior.
2. **Confirm it fails for the RIGHT reason** — run it and read the failure. A test
   that fails with `ReferenceError` or a typo is not a real red; it must fail
   because the *behavior* is missing. This step is the one most often skipped, and
   skipping it is how you end up with tests that never actually tested anything.
3. **Green** — write the *minimal* code to make it pass. Not the general solution,
   not the gold-plated version — just enough.
4. **Refactor** — clean up with the test as a safety net.
5. Repeat.

## Why "right reason" matters

A test that passes the moment you write it tested nothing. A test that fails for the
wrong reason (missing import, wrong fixture) gives a false sense of coverage once
you "fix" it. Always watch the red happen, read the message, and confirm it points
at the absent behavior — *then* implement.

## Example

```ts
// 1. Red — behavior doesn't exist yet.
test("formats a zero balance as $0.00", () => {
  expect(formatBalance(0)).toBe("$0.00");
});
// Run it. It must fail with "formatBalance returned undefined" or similar —
// i.e. the behavior is missing — NOT "formatBalance is not defined" only because
// you forgot to import it.

// 2. Green — minimal.
export function formatBalance(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
```

Pairs with `test-discipline` (what makes a test trustworthy) and
`systematic-debugging` (the same reproduce-first discipline for bugs).
