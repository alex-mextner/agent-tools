---
name: deferred-findings-tracking
description: Use when you notice a bug, gap, or improvement that you're deliberately not fixing right now. Create a tracking issue immediately — never silently drop the finding or leave it only in a chat message.
---

# Deferred findings become tracked issues, immediately

In the middle of a task you'll spot things you shouldn't fix right now — an unrelated
bug, a follow-up, a risky refactor that's out of scope. The right move is to *defer*
it, not to *forget* it. A finding that lives only in your head or in a chat scrollback
is a finding that's gone the moment the session ends.

## Rule

The instant you decide "not now", create a durable tracking record — an issue, a
ticket, a TODO in the project's tracker — with:

- **What** the finding is, concretely (not "improve error handling" but "the upload
  handler swallows network errors and returns 200; user sees a silent failure").
- **Why** it matters / the impact.
- **Where** it lives (file, function) so the next person can find it.

Then continue the original task. Don't inline-fix it (that's scope creep — see
`smallest-change`), and don't leave it as a floating `// TODO` with no owner (see
`comment-hygiene`).

## Why

This is the same principle as `promise-durable-action`: an intention without a
mechanism evaporates. A tracked issue survives the context reset, is visible to the
team, and can be prioritized. "I'll remember to come back to it" is how real bugs ship
to production — the deferral was correct, the lack of a record was the mistake.
