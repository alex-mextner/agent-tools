# Make illegal states unrepresentable

## Avoid

Bags of optional fields and booleans whose combinations can contradict each other, plus broad dictionary/object contracts that erase ownership.

## Prefer

Use discriminated unions, literal unions, named owner interfaces, and exhaustive switches. Model one valid state per variant.

## Why

A precise type removes whole classes of runtime checks. This is broader than anti-slop: anti-slop rejects several common broad-contract symptoms, while the compiler-level policy defines the desired domain model.
