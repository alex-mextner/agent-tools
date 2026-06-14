---
name: ai-review-before-commit
description: Use before committing a non-trivial or architectural change. Run an automated code review on the diff (ideally a model different from the one that wrote it) and address its findings before you commit, treating it as peer review rather than a rubber stamp.
---

# AI review before commit

For anything beyond a trivial fix, get a second pair of eyes on the diff before it
lands. An automated review on the uncommitted change catches the class of mistake the
author is blind to — exactly because they wrote it (see `gan-critic-loop`).

## Rule

- Before committing a non-trivial change, run a review tool over the **uncommitted
  diff** and read its findings.
- Prefer a **different model** as the reviewer than the one that produced the code —
  different blind spots catch different bugs. For architectural calls, a multi-model
  panel beats any single reviewer.
- Treat findings as **peer review, not gospel**: verify each one, fix the real
  problems, and push back (with reasoning) on the ones that are wrong. Performative
  agreement with a bad suggestion is as harmful as ignoring a good one.

## When to spend the call

Worth it: a non-obvious refactor, a new API surface, a security-relevant change, a
weird failure class, "should this ship?" at the end of a feature.

Skip it: a one-line typo fix, a comment tweak, a version bump. Don't burn a review on
trivia — it trains you to ignore the output.

## Why

Review is the cheapest place to catch a bug — before it's in history, before CI,
before a teammate builds on it. An independent reviewer imports a perspective the
author structurally lacks. Wiring it into your pre-commit habit (see
`pre-commit-gate`) makes "did anyone else look at this" the default, not the
exception. The `mcp/review` slot makes such a reviewer callable from any agent.
