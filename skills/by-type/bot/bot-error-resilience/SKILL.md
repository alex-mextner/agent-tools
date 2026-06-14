---
name: bot-error-resilience
description: Use when building a long-running bot. Wrap every command/callback handler so an error logs and replies gracefully instead of crashing, and install process-level handlers that log-and-continue rather than letting one transient error kill the bot.
---

# Bot error resilience: never crash on a transient

A bot is a long-running process serving many users. One unhandled error in one
handler must not take down the whole bot for everyone. Two layers protect it: a wrapper
around each handler, and process-level safety nets.

## Layer 1: wrap every handler

Wrap command/callback/scene handlers in a helper that catches, logs with context, and
replies with a user-friendly message — never leaking an internal error or stack to the
user:

```ts
function safeCommand(name: string, handler: Handler): Handler {
  return async (ctx) => {
    try {
      await handler(ctx);
    } catch (err) {
      log.error({ err, command: name, chatId: ctx.chatId }, "command failed");
      await ctx.reply("Something went wrong. Please try again.").catch(() => {});
    }
  };
}
```

Note `{ err }` (preserves the stack), not `{ error: String(err) }`, and the `.catch()`
on the reply so a failed *reply* doesn't throw out of the catch.

## Layer 2: process-level safety nets

Install handlers for the errors that escape everything else, and **log-and-continue**
rather than crash:

```ts
process.on("unhandledRejection", (reason) => log.error({ err: reason }, "unhandledRejection"));
process.on("uncaughtException",  (err)    => log.error({ err }, "uncaughtException"));
```

For a bot, log-and-continue is usually right: a single transient (a flaky network call,
one malformed update) shouldn't restart the process and drop every active conversation.
(For a *fatal* invariant violation, crashing to be restarted clean can be correct —
judge per error class. Persisted state, see `bot-fsm-state`, makes the restart safe.)

## Why

Without the handler wrapper, one user's edge case throws, the framework's default
behavior kicks in, and at best you've leaked a stack trace, at worst crashed the bot.
Without the process-level net, an un-awaited rejection deep in a library can take the
whole process down. Together they keep one bad update from becoming an outage. Pairs
with `backend/no-silent-catch` — these catches *log*, they don't swallow.
