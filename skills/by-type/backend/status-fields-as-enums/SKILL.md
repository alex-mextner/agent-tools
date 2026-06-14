---
name: status-fields-as-enums
description: Use when typing a field that holds one of a fixed set of values — status, state, role, kind, type. Model it as a union type or enum, never as a bare string.
---

# Status / state / role fields are unions, not strings

A field typed as `string` that actually holds one of `"pending" | "active" | "closed"`
throws away every guarantee the type system could give you. A typo (`"actve"`) compiles.
An unhandled case in a switch compiles. A renamed value leaves stale strings scattered
across the code with no error.

## Rule

Type the field as the **exact set of legal values**:

```ts
// BAD — string accepts anything; no exhaustiveness, no typo protection.
interface Order { status: string; role: string; }

// GOOD — a union (or enum) of the legal values.
type OrderStatus = "pending" | "paid" | "shipped" | "cancelled";
type Role = "owner" | "admin" | "member";
interface Order { status: OrderStatus; role: Role; }
```

Now the compiler:

- rejects a typo or an illegal value at the assignment;
- enforces **exhaustive handling** — a `switch` over the union that misses a case fails
  to compile (with a `never` default), so adding a new status forces you to handle it
  everywhere;
- gives autocomplete the real set of options.

Keep the persisted form (DB column) constrained too — a `CHECK` constraint or an enum
column mirrors the union so bad values can't enter from outside the typed code.

## Why

Status/role/kind fields are exactly the fields whose set of values *changes over time*
(a new order state, a new role). A bare `string` makes every such change a silent risk —
nothing tells you which of the dozen `switch` statements you forgot to update. A union
turns "find every place that handles status" from a fragile grep into a compiler error
list. Pairs with `universal/no-type-escape-hatches`.
