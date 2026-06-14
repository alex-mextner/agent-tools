---
name: migrations-append-only
description: Use when changing a database schema. Add a new migration; never edit or renumber an existing one. Make each migration idempotent, and keep the canonical schema file and the migration in the same change.
---

# Migrations are append-only and idempotent

A migration that has run anywhere — a teammate's machine, staging, production — is
history. Editing or renumbering it means environments that already applied the old
version silently diverge from those that apply the edited one, and there's no error to
warn you.

## Rules

- **Append, never edit.** Need a change to a past migration's effect? Write a *new*
  migration that alters the result. The applied migrations are immutable.
- **Never renumber.** Migration order is a fixed sequence. Inserting or renumbering
  breaks the applied-migration bookkeeping for everyone who already ran them.
- **Make each migration idempotent** so re-running it (after a partial failure, or on an
  environment that's partway) is safe:

  ```sql
  CREATE TABLE IF NOT EXISTS …;
  ALTER TABLE t ADD COLUMN IF NOT EXISTS …;   -- or guard with a columnExists() check
  ```

- **Keep the canonical schema and the migration in step, in the same commit.** If you
  maintain a `schema.sql` (the current full shape) alongside incremental migrations,
  update both together — otherwise a fresh DB built from `schema.sql` diverges from one
  built by replaying migrations.

## Why

Migrations are a distributed, replayed log: every environment applies them in order, and
they assume each one is the same everywhere it ran. Editing breaks that assumption
silently — the schema "drift" surfaces later as a column that exists in prod but not in
the migration history, or vice versa. Append-only + idempotent + schema-in-sync keeps
every environment derivable from the same source of truth.
