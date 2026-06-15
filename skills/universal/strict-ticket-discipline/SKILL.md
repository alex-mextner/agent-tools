---
name: strict-ticket-discipline
description: Use before starting any non-trivial change — a feature, a bugfix, a refactor. Every such change starts from a ticket (task-cli / GitHub Issue / Linear) with acceptance criteria, motivation, and user-impact written down before work begins, and the commit references it.
---

# Every non-trivial change starts from a ticket

Code without a ticket is a change nobody agreed to. The ticket is where the
*why* lives — the motivation, who it's for, and what "done" means. Write that
down **before** you touch code, not after. A commit that can't point at a ticket
is a commit whose intent exists only in a chat scrollback that's gone the moment
the session ends.

## The rule

Before starting a non-trivial change, open a ticket (`task` / GitHub Issue /
Linear) that states:

- **Motivation** — why this change, what problem it solves. Not "improve X" but
  the concrete failure or gap.
- **Acceptance criteria** — the checklist that makes it *done* and verifiable.
  If you can't write these, the change isn't understood yet.
- **User-impact** — who is affected and how. "Internal refactor, no user-facing
  change" is a valid answer — but it's an answer you wrote down, not skipped.

Then reference that ticket from the work: in the commit message (`Refs #123`,
`task:ABC-12`, a Linear `ENG-456`) and/or the PR. The `require-ticket-before-commit`
agent-hook checks for that reference at commit time.

## What counts as "non-trivial"

Use a ticket for: features, bugfixes, refactors, behavior changes, anything that
ships to a user or another consumer. **Skip** the ticket for genuinely trivial
chores — a typo fix, a comment, a lockfile bump, a formatting-only commit. The
hook treats `chore:`/`docs:`/`style:` commits and `wip`/fixup commits as exempt
by default (see its README), so the gate doesn't punish the small stuff.

## Why before, not after

A ticket written *after* the code is a description of what you built, not a
contract for what you should build. Writing acceptance criteria first is the same
discipline as `tdd-red-first`: stating the target before the implementation keeps
the implementation honest. It also lets someone push back *before* the work is
sunk, not after.

This pairs with `deferred-findings-tracking` (a finding you defer becomes a
ticket too) and `promise-durable-action` (an intention without a durable record
evaporates). The ticket is that durable record for planned work.

## Common mistakes

- **Retroactive ticket** — filing the issue right before the commit, copying the
  diff into it. That's a receipt, not a spec. Write it first.
- **Empty acceptance criteria** — "make it work" is not a criterion. List the
  observable conditions that prove it's done.
- **Ticket reference only in chat** — the reference must be in the commit/PR, not
  just something you told the reviewer. Chat scrollback doesn't survive.
- **Over-ticketing trivia** — a one-character typo fix doesn't need a ticket.
  Reserve the discipline for changes someone would want to have agreed to.
