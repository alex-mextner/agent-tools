# No `any`

## Avoid

Using `any` to silence a type error, accepting `any` in application contracts, or laundering it through casts.

## Prefer

Use the real domain type. At genuinely untyped I/O, receive `unknown`, immediately parse/decode it with the expected boundary contract, and pass the resulting named type inward.

## Why

`any` disables checking transitively. `unknown` is safer only while it represents real uncertainty; carrying it through ordinary application layers merely postpones validation and encourages ad-hoc narrowing.
