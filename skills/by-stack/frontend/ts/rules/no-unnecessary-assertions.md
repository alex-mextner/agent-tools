# No unnecessary type assertions

## Avoid

Assertions that do not change the expression's type, including annotations/casts added only to make code look more explicit.

## Prefer

Remove them and let inference carry the already-known type. Enable type-aware `typescript/no-unnecessary-type-assertion`.

## Why

Redundant assertions add visual noise and normalize the idea that assertions are ordinary documentation. Keeping the assertion count low makes the genuinely exceptional, justified assertions stand out.
