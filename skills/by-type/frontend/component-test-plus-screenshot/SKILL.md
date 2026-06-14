---
name: component-test-plus-screenshot
description: Use for every user-visible UI change. Pair a behavioral test (what the user can do) with a screenshot review cycle (what the user sees) — neither alone proves the change is correct.
---

# Behavioral test + screenshot review, for every visible change

A UI change has two correctness dimensions, and they need different proofs:

- **Behavior** — can the user do the thing? (click, type, submit, the right handler
  fires, the right state updates). This is what a behavioral test verifies.
- **Appearance** — does it *look* right? (layout, spacing, theme, nothing clipped or
  blank). No unit test can see this; only looking at a render can.

Do both.

## Behavioral test: test what the user does, not internals

```tsx
// Assert on user-observable behavior, not on internal state or implementation.
test("submitting the form shows a success message", async () => {
  render(<SignupForm />);
  await user.type(screen.getByLabelText("Email"), "a@b.com");
  await user.click(screen.getByRole("button", { name: "Sign up" }));
  expect(await screen.findByText("Check your inbox")).toBeVisible();
});
```

Query by role/label/text (what the user perceives), trigger real interactions, assert on
visible outcomes. Don't reach into component state or props — those are implementation
details that should be free to change.

## Screenshot review cycle

Capture the rendered component, **look at the capture**, review it critically, fix what's
off, re-capture. See `universal/visual-proof-cycle` for the full loop — it applies
directly here. A passing behavioral test with a broken layout is a half-verified change.

## Why

Behavioral tests catch logic regressions but are blind to a misaligned, mis-themed, or
invisible element. A screenshot catches the visual but not whether the button actually
does anything. Together they cover both ways a UI change can be wrong; either alone leaves
a gap that ships.
