---
name: strict-ticket-discipline
description: Use before starting any non-trivial change — a feature, a bugfix, a refactor — and whenever you file a ticket or tracked task. Every change starts from a ticket (task-cli / GitHub Issue / Linear) written before work and referenced from the commit. The ticket must be self-explanatory cold: what is concretely wrong, evidence captured FIRST (a Playwright/Docker screenshot of the broken state, never screencapture; a failing run; a repro), where it came from, the user-facing consequence of not fixing it, provable acceptance criteria, and pseudocode when the fix is non-obvious.
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

## What a ticket must contain — the evidence-first standard

Motivation, acceptance, and user-impact are the floor. A ticket worth filing is
*self-explanatory to someone with no prior context* — the person who picks it up
(a teammate on a phone, another agent cold) should not have to re-derive a single
thing. A vague ticket gets rejected. Write these sections, **in this order**:

1. **What is concretely wrong** — the specific defect or gap, pinned with
   `file:line` or the exact user action that misbehaves. Not "X is suboptimal" —
   "X does Y when it should do Z".
2. **Evidence — capture what is broken, and how, FIRST, at creation time.** This is
   the rule that makes the difference. The moment you identify a problem worth a
   ticket, record the proof *before moving on to anything else* — never "I'll add a
   screenshot later":
   - **User-observable UI behavior** → a SCREENSHOT of the broken state, taken via
     Playwright or the project's Docker e2e harness. **Never `screencapture`** — a
     desktop grab depends on permissions and the active screen and breaks silently;
     drive the capture through the browser/Electron over CDP instead (this is the
     `visual-proof-cycle` capture, applied at ticket time). The "before" (broken
     state) is mandatory at creation; the "after" follows once the fix lands.
   - **CI / gate failure** → the failing run output or the finding count (e.g. the
     red Semgrep scan, the exact failing assertion).
   - **Pure code / logic** → the offending snippet (`file:line`) plus a failing
     repro that demonstrates the bug.
   - If a proof genuinely cannot exist yet (an unbuilt feature has no "after"), say
     so explicitly and capture the CURRENT / broken behavior instead. Being explicit
     about what is observable *now* is not the same as skipping evidence.
3. **Where this came from** — the origin: the session, audit, review, or brainstorm
   that surfaced it, plus the related ticket / PR / migration or decision that caused
   it. So no one has to reconstruct the trail.
4. **What happens if we don't do it** — the concrete consequence of leaving it
   unfixed, and **how it hurts the user** specifically. Not abstract "tech debt" —
   the actual broken thing the user hits.
5. **Acceptance criteria** — a checkbox list of verifiable conditions, each provable
   by a test or an observable behavior, and each naming its proof method (an e2e
   spec, a CI scan, a unit test). This is the acceptance checklist from "The rule"
   section above, sharpened: every item says *how* you'll prove it.
6. **Pseudocode** — when the fix shape is non-obvious, the minimal pseudocode of the
   change: the seam, the key branch, the data flow. Omit it only when the change is
   truly trivial.

This standard applies to Linear tickets, `TaskCreate` / `task` tasks, GitHub issues,
and any other tracked work item.

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
ticket too — and it carries the same evidence-first standard) and
`promise-durable-action` (an intention without a durable record evaporates). The
ticket is that durable record for planned work. Capturing the broken-state proof at
ticket time is `visual-proof-cycle` applied early; trusting that proof rather than
your own assertion is `adversarial-verification`.

## Do not

- **Do NOT ask "should I capture a screenshot / evidence?"** — capture it. Do it as
  the work surfaces the problem, not as a follow-up question.
- **Do NOT file a one-liner "fix X" and move on.** If it is worth a ticket, it is
  worth the sections above.
- **Do NOT claim a proof exists when it does not** — be explicit about what is
  observable now versus only after the fix.

## Common mistakes

- **Retroactive ticket** — filing the issue right before the commit, copying the
  diff into it. That's a receipt, not a spec. Write it first.
- **"I'll add the screenshot later"** — the evidence is the part that decays
  fastest. The broken state you can reproduce now may be gone after the fix or after
  the environment moves on. Capture it at creation time.
- **Empty acceptance criteria** — "make it work" is not a criterion. List the
  observable conditions that prove it's done, each with its proof method.
- **Ticket reference only in chat** — the reference must be in the commit/PR, not
  just something you told the reviewer. Chat scrollback doesn't survive.
- **Over-ticketing trivia** — a one-character typo fix doesn't need a ticket.
  Reserve the discipline for changes someone would want to have agreed to.
