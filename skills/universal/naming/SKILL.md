---
name: naming
description: Use when naming functions, types, variables, or files. Name by what a thing IS or DOES, not by its current implementation detail or its position in time.
---

# Naming: describe what, not how

A name is read far more often than it is written, and it outlives the implementation
it was named after. Name for meaning, not mechanism.

## Don't bake the implementation into the name

If you name a type after the library or technique it currently uses, the name lies
the moment you swap the implementation.

```ts
// BAD — couples the name to today's implementation.
class ZodValidator { ... }      // what if you drop zod?
const redisCache = ...          // what if it moves to memory?
function parseWithRegex() { ... }

// GOOD — names the role.
class Validator { ... }
const cache = ...
function parse() { ... }
```

The exception: when two implementations genuinely coexist and the distinction is the
point (`MemoryCache` vs `RedisCache` behind a `Cache` interface). Then the mechanism
*is* the meaning.

## No temporal names

`UserServiceV2`, `newHandler`, `legacyParse`, `processData_old` — "new" becomes old,
"v2" becomes the only version, and the name stops meaning anything. Name for the
behavior; use version control for history.

## Describe behavior, not internals

A function name should let a caller predict what it does without reading the body:
`fetchActiveUsers()` beats `doUserStuff()`; `assertWithinBudget()` beats `check()`.

## Why

Good names are the cheapest documentation you have and the first thing that goes
stale. Naming by role instead of implementation means the name survives refactors,
and the next reader trusts it.
