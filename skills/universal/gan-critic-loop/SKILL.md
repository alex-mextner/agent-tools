---
name: gan-critic-loop
description: Use when an agent generates work and then judges its own quality — code review, output verification, "is this good enough". The generator and the critic must be separate agents/models; self-evaluation is biased and passes its own mistakes.
---

# Separate the worker from the critic

A model evaluating its own output is grading its own homework. It shares the same
blind spots that produced the work, so it systematically misses the same mistakes and
rates its output higher than an independent judge would. To get an honest verdict, the
critic must be a *different* agent — ideally a different model.

## Rule

- The agent that **produces** the work and the agent that **judges** it are
  distinct. Don't ask one model "is your answer correct?" and trust the yes.
- For meaningful checks, use a *different model* as the critic — different training,
  different blind spots, so it catches what the generator can't see in itself.
- For high-stakes calls, use **several** critics and look at where they agree and
  disagree (a quorum), rather than trusting any single verdict.
- Treat the critic's feedback as peer review, not gospel: verify its claims, push
  back on the ones that are wrong. (See `requesting-code-review` /
  `receiving-code-review` if available.)

## Where this applies

- **Code review before merge** — a separate review model on the diff, not the author
  re-reading their own work. See the `review` CLI and `pre-commit-gate`.
- **Visual verification** — a vision model judging a render the generator produced.
  See `visual-proof-cycle`.
- **Brainstorming / design** — rotating expert personas critiquing each other beats
  one model arguing with itself.

## Why

The whole value of review is an outside perspective. Self-review keeps the
perspective inside, where the bug already hid. Separating worker and critic is the
cheapest way to import a perspective the generator structurally cannot have.
