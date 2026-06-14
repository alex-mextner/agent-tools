---
name: subagent-handoff-contract
description: Use when orchestrating subagents that each do a unit of work and report back. Require each subagent to return a fixed handoff contract — branch/commit, changed files, tests run, review status, merge blockers, next action — so the orchestrator can act without re-investigating.
---

# Subagent handoff contract

When an orchestrator dispatches subagents to do parallel work, a vague "done!" report
forces the orchestrator to re-investigate everything — what branch, did tests pass, can it
merge. Require every subagent to return a **fixed, structured handoff** so the next step is
mechanical.

## The contract

Each subagent ends by reporting, in a consistent shape:

- **Branch / commit** — exactly where the work landed (`work/feature-x @ abc1234`). Not
  "I committed it" — the ref.
- **Changed files** — the list, so the orchestrator can reason about overlap/conflicts.
- **Tests** — what was run and the result (`unit: 42 passed; e2e: not run`). Evidence,
  not "tests pass".
- **Review status** — whether a review ran and its verdict (`codex review: 2 findings
  addressed`), or "not reviewed".
- **Merge blockers** — anything preventing merge (failing CI, an unresolved conflict, a
  pending decision), or "none".
- **Next action** — the single recommended next step (`ready to merge` / `needs human
  decision on X` / `blocked on dependency Y`).

```
branch:        work/parser-fix @ 9f2a1c
changed:       src/parser.ts, test/parser.test.ts
tests:         unit 18/18 pass; lint clean
review:        codex — 1 finding (null-guard), fixed
blockers:      none
next action:   ready to merge
```

## Why

A structured handoff makes orchestration *composable*: the orchestrator can decide to
merge, serialize a dependent task, or escalate — purely from the report, without re-running
the subagent's investigation. Free-form "done" reports force re-discovery at every hop,
which is where parallel-agent orchestration bogs down. The contract also surfaces the
things subagents quietly skip (did review actually run? are there blockers?) by making
them required fields. Pairs with `parallelize-independent` and `worktree-isolation`.
