---
name: gramio-handler-ordering
description: Use when registering Telegram bot handlers (GramIO or similar frameworks). Register command handlers BEFORE a catch-all message handler — commands are messages too, so a catch-all registered first will swallow them.
---

# Register commands before the message catch-all

In Telegram bot frameworks, a slash command (`/start`) arrives as a regular text
message. Handlers run in registration order, so if you register a catch-all
`bot.on("message")` *before* your command handlers, the catch-all matches first,
consumes the update, and your command handlers never fire. The bug looks like "my
commands stopped working" with no error.

## Rule

Register specific handlers first, the catch-all last:

```ts
// 1. Specific command handlers FIRST.
bot.command("start", startHandler);
bot.command("help", helpHandler);

// 2. The catch-all message handler LAST — it only sees messages that no
//    command handler claimed.
bot.on("message", fallbackHandler);
```

If your framework lets a handler decide whether to pass the update on, make sure the
catch-all explicitly *doesn't* swallow command messages — but ordering is the simpler
and more reliable fix.

## Why

The ordering bug is silent and confusing: nothing throws, the bot is "running", and
the commands simply vanish. Understanding that commands *are* messages — and that
first-match-wins ordering applies — turns a mysterious dead command into an obvious
one-line fix. Make "catch-all goes last" a fixed rule and the class of bug disappears.
