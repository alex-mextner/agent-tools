---
name: structured-exit-codes
description: Use when a CLI command fails. Exit with a meaningful, stable exit code per failure class, and print an error that says what went wrong, why, how to fix it, and (if relevant) the install command — so both humans and scripts can act on it.
---

# Structured exit codes + actionable errors

A CLI is consumed by humans *and* by scripts/CI. Exiting `1` for everything tells a
script nothing, and printing a bare `Error: failed` tells a human nothing. Give both
audiences something to act on.

## Stable exit codes per failure class

Assign a distinct, documented exit code to each *class* of failure, so a calling script
can branch on it:

```
0    success
1    gate failure (the operation ran but the result didn't pass — lint/test/check failed)
2    invalid argument / usage error
127  missing dependency (a required external tool/binary isn't installed)
```

(127 for "command not found" follows shell convention.) Keep them stable — scripts depend
on them — and document them in `--help`.

## Actionable error messages

Every error should carry four things:

```
error: cannot render — `openscad` is not installed   ← WHAT failed
this command needs OpenSCAD to produce the mesh.       ← WHY it's needed
install it with:  brew install openscad                ← HOW to fix (the install command)
then re-run:      cli render model.scad                ← the remediation
```

Model this with structured error types that carry the class (→ exit code), the message,
the cause, and the remediation/install hint — so the formatting is consistent and the
exit code can't drift from the message.

## Why

The exit code is the machine-readable verdict; the message is the human-readable one. A
script can retry a `127` (install the dep) but should fail a `2` (bad usage) — only
possible if the codes are distinct and stable. And a human staring at a failure wants the
fix, not just the symptom — the install command in the error turns a dead end into a next
step. Pairs with `cli/idempotent-bootstrap` (which can auto-fix some 127s).
