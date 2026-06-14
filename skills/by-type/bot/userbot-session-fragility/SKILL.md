---
name: userbot-session-fragility
description: Use when running a Telegram userbot / MTProto client (Pyrogram, Telethon, GramJS) that authenticates as a user account rather than a Bot API bot. The single session file is fragile under concurrent access; treat it as a single-writer resource and back it up.
---

# MTProto userbot session fragility

A userbot (MTProto client logging in as a *user* account, not a Bot API bot) keeps its
authorization in a single session store — typically one SQLite file. That file is the
whole login: lose or corrupt it and you must re-authenticate the account from scratch,
with all the friction (and risk) that re-login entails.

## The trap

The session store is **not safe under concurrent writers**. Two processes (or two
client instances) opening the same session file, or aggressive journal/WAL settings
under concurrent access, can corrupt it. A corrupted session is unrecoverable from the
file alone — the client refuses to start and demands a fresh login.

## Rules

- **Single writer.** Exactly one process owns the session at a time. Don't run two
  instances against the same session file; don't let a deploy start a new instance
  before the old one has released it. Serialize access.
- **Back up the session.** Keep a copy of a known-good session so a corruption is a
  restore, not a re-authentication. Treat the backup as sensitive — it *is* the account
  login.
- **Handle the session as a secret.** It grants full access to the account. Never log
  its contents, never commit it, store it with the same care as a private key (see
  `backend/secret-handling`).
- **Plan for re-auth.** Despite the above, sessions do occasionally die. Have a
  documented, low-friction re-authentication path so a dead session is an inconvenience,
  not an outage.

## Why

Unlike a Bot API token (which you can regenerate freely), a userbot session represents
an *account* login that's painful and rate-limited to re-establish, and a corrupt file
gives no partial recovery. Knowing the file is single-writer and fragile — and keeping a
backup — turns "the bot won't start and wants me to log in again at 2am" into a restore
from backup.

## Note

This skill is the *concept* — single-writer, fragile, back it up, treat as a secret. The
recovery mechanics and key handling for any specific deployment are infrastructure
details that belong in that project's private runbook, not here.
