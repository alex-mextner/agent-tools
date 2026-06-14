---
name: task-completion-selfcheck
description: Use when you believe a task is done, before reporting it complete. Run an explicit two-question self-check — did I finish everything asked, and do I have concrete follow-up ideas — instead of declaring victory on the happy path.
---

# Completion self-check

"Done" is a claim that's easy to make and easy to get wrong. Before reporting a task
complete, run an explicit self-check rather than stopping at the first thing that
works.

## Two questions, out loud

1. **Did I finish everything? Did I miss anything?** Walk back through *every* item
   in the original request and confirm each is actually handled. Not just the code —
   the commits, the push, the deploy, the docs, the cleanup, the artifacts. The thing
   most often missed is the second or third clause of the request, not the first.

2. **Do I have concrete ideas for what else to do?** Not vague "could be improved" —
   specific items: a bug you noticed in passing, a follow-up worth a ticket, dead
   code to clean up. Phrase them as "X is worth doing because Y", and either do them
   or record them as tracked follow-ups.

## Verify, don't assert

A completion claim must be backed by evidence you actually produced — the test output
you ran, the screenshot you looked at, the command whose exit code you read. "It
should work" is not "it works". (See `verification-before-completion` if available,
and `visual-proof-cycle` for UI.)

## Why

The gap between "the main change works" and "the task is done" is where dropped
sub-requests, unpushed commits, and forgotten cleanup live. A fixed two-question
ritual catches them before the report goes out, instead of after the user finds them.

This can be enforced for agents via a Stop-hook that injects the two questions; see
`agent-hooks/stop-completion-selfcheck`.
