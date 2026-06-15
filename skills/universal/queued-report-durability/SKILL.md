---
name: queued-report-durability
description: Use when you owe a status report or result to an external channel (Telegram, Slack, email, a dashboard, a webhook) and that channel — or its delivery tool or credentials — is unavailable or the send fails. Never silently treat the report as delivered; surface the failure, include the full text, and durably queue it.
---

# A failed delivery is not a delivery — queue it, don't fake it

## Overview

When a report's delivery channel is down (the tool is missing, the credentials/recipient
config aren't there, or the send errors), the tempting move is to behave as if it went
out — or to drop it. Either way the report now exists *nowhere durable*, the recipient is
blind, and you've claimed a success that didn't happen.

## Rule

If the delivery tool, channel, or recipient config is unavailable, or the send fails:

0. **Try a cheap recovery first** — the channel is often just an unset env var / missing
   config in a non-interactive shell. A real delivery beats a queued one; only queue once
   an actual send has genuinely failed.
1. **Say so explicitly** in whatever channel you DO have (the chat/session/log): state
   that delivery failed and why.
2. **Include the full report text inline** so it isn't lost — never collapse it to "sent
   the report" when you didn't.
3. **Durably queue it**: append the report to a known notes file (e.g.
   `docs/notes/queued-<channel>-reports.md`) or the active status note, with a timestamp,
   the intended recipient, and the complete text. Then **confirm the write landed** — the
   durable write has the same "did it actually succeed" trap as the send.
4. **Drain it later**: re-send when the channel is restored. A queued report that is never
   re-sent is still undelivered.

Never claim or assume a delivery you did not confirm succeeded.

## Why

A faked or dropped delivery is silent data loss. The recipient never sees the update, and
worse, your own report says "done / delivered," so nobody knows it's missing until they go
looking. The whole point of a report is that it reaches someone durably; if the channel
can't carry it right now, the report's text and intent must survive somewhere that will.

Pairs with `promise-durable-action` (turn "I'll re-send it later" into an actual queued
artifact, not a verbal promise) and `task-completion-selfcheck` (don't mark the report
step done on the strength of an unconfirmed send).
