---
name: durable-scheduled-jobs
description: Use when a bot or service needs periodic or delayed work — reminders, digests, retries, scheduled sends. Use a durable job queue with persisted repeating jobs, never setInterval/setTimeout, which vanish on restart.
---

# Durable scheduled jobs, not setInterval

`setInterval` / `setTimeout` schedule work in process memory. When the process
restarts — deploy, crash, scale event — every pending timer is gone, silently. A daily
digest scheduled with `setInterval` simply stops firing after the next redeploy, and
nobody notices until users complain the digest disappeared.

## Rule

Use a **durable job queue** (e.g. BullMQ with a Redis backing store, or your stack's
equivalent) for anything periodic or delayed:

- **Repeating jobs** (a daily digest, an hourly poll) are registered as repeatable jobs
  in the queue, which persists the schedule. A restart re-attaches to the existing
  schedule instead of losing it.
- **Delayed jobs** (remind in 1 hour, retry in 5 minutes) are enqueued with a delay and
  survive a restart in the store.
- The worker is idempotent so a job that runs twice (after a crash-and-resume) doesn't
  double-act.

```ts
// Durable repeating job — survives restarts because the schedule lives in the store.
await queue.add("daily-digest", {}, { repeat: { pattern: "0 9 * * *" } });

// NOT this — dies on the next restart, silently.
// setInterval(sendDigest, 24 * 60 * 60 * 1000);
```

## Why

Memory timers are fine for a script you run by hand and never restart. A long-running
bot restarts routinely, and each restart wipes its in-memory schedule with no error and
no log line — the worst kind of failure to debug, because the *code* is fine, the
*timer* just no longer exists. A persisted queue makes the schedule a durable fact, not
a property of one process's uptime. This is the bot-flavored case of
`promise-durable-action`: a scheduled intention needs a durable mechanism.
