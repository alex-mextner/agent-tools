---
name: atomic-db-transactions
description: Use when a database operation reads a value and then writes based on it (check-then-act), or touches multiple rows that must stay consistent. Do the read and the dependent write in one transaction, or two concurrent requests will corrupt the data.
---

# Multi-step DB operations must be atomic

A `SELECT` followed by an `UPDATE` based on what you read is two operations with a gap
between them. Under concurrency, a second request can run *in that gap*, and both writes
proceed on stale reads — the classic lost-update / double-spend / oversold-inventory
bug. It passes every single-threaded test and corrupts data the moment two requests
race.

## Rule

Do the read and the dependent write in **one transaction**, and lock or guard the rows
so concurrent writers serialize:

```ts
await db.transaction(async (tx) => {
  // Read and write the SAME rows inside the transaction.
  const acct = await tx.accounts.findForUpdate(id);   // row lock / SELECT ... FOR UPDATE
  if (acct.balance < amount) throw new InsufficientFunds();
  await tx.accounts.update(id, { balance: acct.balance - amount });
});
```

Alternatives that achieve the same atomicity:

- A **conditional write** that bakes the check into the UPDATE
  (`UPDATE … SET balance = balance - $amt WHERE id = $id AND balance >= $amt`) and
  checks the affected-row count.
- **Optimistic concurrency** with a version column, retrying on conflict.

The wrong fix is to read, compute in app code, and write back without a transaction or
guard — that's exactly the racy pattern.

## Why

The gap between read and write is invisible in development (requests run one at a time)
and reliably exploited in production (requests run concurrently). Wrapping the
check-then-act in a transaction with row locking — or collapsing it into one conditional
statement — is what makes "two users hit submit at the same instant" safe instead of
catastrophic. Pairs with `no-silent-fallbacks`: don't paper over a failed conditional
write with a default.
