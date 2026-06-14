---
name: specs-are-authoritative
description: Use before touching a feature in a project that keeps written specs. Read the feature's spec first; it is the source of truth for intended behavior. Don't infer intent from the current code, which may itself be the thing that's wrong.
---

# Specs are authoritative — read the spec before touching the feature

In a project that maintains written specs (`docs/specs/<feature>.md` or similar), the
spec — not the current code — is the authority on what the feature is *supposed* to do.
Reverse-engineering intent from the implementation is unreliable: the code might be the
bug you're about to fix, or it might encode a workaround that the spec explains.

## Rule

- Before changing a feature, **read its spec.** It tells you the intended behavior, the
  edge cases that were considered, the decisions that were made and why.
- When code and spec disagree, that disagreement is itself the finding — either the code
  is wrong (fix it) or the spec is stale (update it). Don't silently make the code match
  a spec you didn't read, or change behavior the spec deliberately specified.
- If your change alters intended behavior, **update the spec in the same change** — a spec
  that lags the code stops being authoritative and becomes just another stale doc.

## Why

Code answers "what does it do"; a spec answers "what should it do" — and only the second
tells you whether a behavior is a feature or a bug. Starting from the implementation means
you might "fix" intentional behavior or rebuild a workaround the spec already explained.
Reading the spec first grounds the change in intent. Keeping the spec current (see
`feedback`-style discipline) keeps it worth reading next time. Pairs with
`cli/help-docs-sync` and `library/version-changelog-assert` — same family of "keep the
written record in step with the code".

## Note

A good spec describes what the feature *is*; it shouldn't catalog the absence of things
that never existed. Document present behavior and real decisions, not hypothetical
non-features.
