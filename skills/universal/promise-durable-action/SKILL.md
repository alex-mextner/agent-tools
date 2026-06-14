---
name: promise-durable-action
description: Use whenever you're about to say "I'll do X later", "from now on I'll…", "going forward", or "I'll remember to…". A promise with no durable mechanism behind it is empty. Turn it into a file edit, a scheduled task, or a recorded rule right now.
---

# A promise is an immediate durable action, not words

There are two kinds of promise, and both need a durable mechanism *in the same moment
you make them*:

- **(A) Deferred action** — "I'll do X for future runs", "I'll add this later".
- **(B) Behavioral promise** — "from now on I'll…", "going forward", "I'll always",
  "I won't do that again". This is the dangerous one: it *sounds* immediate but has
  zero mechanism behind it. "I'll just remember to behave better" has no enforcement
  and regresses by the next session.

A behavioral promise with no edit to a rule file, config, or tool is just words.

## Turn any promise into a guarantee, right now, one of three ways

1. **Do it immediately** — make the change in code/config now. The problem disappears
   for good. (Preferred.)
2. **Schedule it** — if it genuinely can't happen now, set a timer / cron / tracked
   task with a concrete trigger so it's guaranteed to resurface and get done.
3. **Encode the rule** — write it into the place that governs the behavior (a skill,
   an `AGENTS.md`, a lint rule, a hook config) — not just into a chat message.

## Tripwire

The words "I'll", "from now on", "going forward", "always", "never again", "later",
"in the future" are the trigger. Before you type one, ask: *is there an edit to a
file/config/tool in this same turn that makes it true?* If not — either make the edit
now, or don't claim it. Report "done: added rule X to file Y", not "I'll do X".

## Why

Memory and good intentions don't survive a context reset. A mechanism does. The
difference between a reliable system and a flaky one is whether its promises are
backed by durable state or by the hope that someone remembers.
