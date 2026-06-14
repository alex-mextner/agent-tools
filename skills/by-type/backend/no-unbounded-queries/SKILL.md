---
name: no-unbounded-queries
description: Use when writing database queries on a code path that runs in production hot loops or against tables that grow. Don't SELECT * and don't fetch whole tables — select needed columns, paginate or cursor-batch large reads, and reserve unbounded findAll() for admin/test code.
---

# No unbounded queries in hot paths

A query that fetches an entire growing table is a time bomb: it's fast with 100 rows in
development and falls over with 10 million in production, taking memory and latency with
it. The query didn't change — the data did.

## Rules

- **No `SELECT *`** on hot paths. Select the columns you actually use. `SELECT *` pulls
  large/unused columns over the wire and breaks the moment someone adds a `blob` column.
- **No "fetch the whole table".** Reads over tables that grow must be **paginated** or
  **cursor-batched** — process a bounded page at a time, not the entire result set in
  memory.

  ```ts
  // BAD — loads every row into memory; fine in dev, fatal at scale.
  const all = await db.users.findAll();

  // GOOD — cursor-batch a bounded chunk at a time.
  for await (const batch of cursorBatches(db.users, { size: 500 })) {
    await process(batch);
  }
  ```

- **`findAll()` / unbounded reads belong in admin/test code only** — places where the
  row count is known-small and not user-facing. Never on a per-request hot path.

## Why

The failure mode is the nastiest kind: zero symptoms until production data grows past
some threshold, then a sudden memory blowup or latency cliff that's hard to trace
because the code "always worked". Bounding reads from the start means the code's cost
scales with the work it does, not with the size of the table. Pairs with
`atomic-db-transactions` for the writes.
