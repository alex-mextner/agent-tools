---
name: gramio-scenes
description: Use when building multi-step Telegram conversation flows with GramIO Scenes (wizards). Two traps bite hard - .extend() must come after .params()/.state(), and step.next() re-processes the current message rather than waiting for the next one.
---

# GramIO Scenes: the two traps

Multi-step "wizard" scenes in GramIO have two non-obvious behaviors that produce
confusing bugs. Both are about *ordering* and *re-entry*.

## Trap 1: `.extend()` must come after `.params()` / `.state()`

The scene builder is order-sensitive. If you call `.extend()` (to add shared
context/derives) before declaring `.params()` and `.state()`, the extension doesn't
see them and you get `undefined` state or missing params at runtime — with no build
error. Always declare params and state first, then extend:

```ts
const scene = new Scene("onboarding")
  .params<{ inviteId: string }>()   // 1. params
  .state<{ name?: string }>()       // 2. state
  .extend(sharedContext)            // 3. THEN extend — sees params + state
  .step(...);
```

## Trap 2: `step.next()` re-processes the CURRENT message

Calling `step.next()` does not "wait for the next user message" — it advances the step
pointer and then re-runs the handler against the message currently in hand. If you
naively `next()` inside a step that just handled input, the next step immediately fires
on the *same* message, skipping the user. Guarding this with a `firstTime` flag or
scene-state field is fragile and races.

The robust pattern is an explicit in-memory transition guard keyed by chat — record
that a transition is pending and let the *next genuine* update consume it:

```ts
// Keyed by chat id; survives within the process, intentionally NOT scene state
// (which gets re-read mid-flow and races).
const pendingStepTransitions = new Map<number, true>();
```

This makes "advance the step" and "process the next message" two distinct events
instead of one fused one.

## Why

Both traps are silent: no error, just a wizard that skips a question or reads
`undefined`. Knowing that the builder is order-sensitive and that `next()` re-enters on
the current message turns hours of "why did it skip step 2" into a known shape. Persist
the durable part of scene state in a store (see `bot-fsm-state`) so a restart mid-wizard
doesn't strand the user.
