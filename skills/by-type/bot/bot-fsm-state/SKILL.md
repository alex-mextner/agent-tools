---
name: bot-fsm-state
description: Use when a bot conversation has more than a couple of steps or branches. Model the flow as an explicit finite-state machine with states/events/actions/guards, and persist the state so a process restart doesn't strand mid-conversation users.
---

# Model bot conversation flow as a persisted FSM

Ad-hoc conversation flow — a tangle of `if (user.step === 3)` checks and in-memory
flags — becomes unmaintainable the moment the flow has branches, back-navigation, or
timeouts, and it loses every active conversation on restart. Model it as an explicit
finite-state machine and persist the state.

## Pattern

- Define the flow as a machine: **states**, the **events** that transition between
  them, the **actions** run on transition, and **guards** that gate transitions. A
  statechart library (e.g. XState v5) makes this declarative and testable; a hand-rolled
  switch on a typed state union works too.
- **Persist the machine's state** (in SQLite or your store), keyed by chat/user, so it
  survives a process restart. An in-memory-only machine drops every in-progress
  conversation when the bot redeploys — users are left mid-wizard with a bot that
  forgot them.
- On startup / next message, **rehydrate** the persisted state and resume.

```ts
// State is a typed union, not a loose number; transitions are explicit.
type ChatState =
  | { name: "idle" }
  | { name: "awaiting_amount"; category: string }
  | { name: "confirming"; draft: Entry };

// Persisted per chat so a restart resumes instead of stranding the user.
await store.saveState(chatId, nextState);
```

## Why

An explicit FSM makes the legal transitions obvious, makes illegal ones
unrepresentable, and is testable without a running bot — you assert state→event→state
directly. Persistence is the other half: a bot *will* restart (deploys, crashes), and
without persisted state every active multi-step flow silently breaks. Together they
turn "why did the wizard forget where it was" into a non-issue.

Pairs with `gramio-scenes` (the scene-specific re-entry traps) and
`bot-error-resilience` (surviving the transient crash that triggers the restart).
