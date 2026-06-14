---
name: yagni-kiss-dry
description: Use when designing or adding code. Build what the task needs now (YAGNI), keep it simple (KISS), and check for an existing type/util/component before creating a new one (DRY). Don't add abstraction for hypothetical futures.
---

# YAGNI / KISS / DRY — in that order of caution

Three old principles, each guarding against a different way to over-engineer.

## YAGNI — You Aren't Gonna Need It

Build for the requirement in front of you, not for an imagined future. A "flexible"
config system for the one value you have today, a plugin interface for the one
implementation that exists, a generic abstraction "in case we add more later" — these
cost real complexity now to serve a future that usually never arrives, or arrives
differently than you guessed. Add the abstraction when the *second* case actually
shows up.

## KISS — Keep It Simple

The simplest thing that correctly solves the problem is almost always the right one.
Clever, condensed, or deeply layered code is harder to read, debug, and change. Optimize
for the next reader, not for elegance points.

## DRY — Don't Repeat Yourself (carefully)

Before creating a new type, util, validator, or component, check whether one already
exists — reuse beats reinvention (see `shared-util-single-source`). But DRY is the one
to apply *with* judgment: collapsing two things that merely *look* similar but serve
different purposes creates a worse coupling than the duplication did. Deduplicate real
duplication, not coincidental resemblance.

## Why

Each principle fails in a recognizable way: YAGNI-violations are abstractions with one
caller; KISS-violations are code you have to re-read three times; over-applied DRY is a
helper with five boolean flags serving five callers that should have stayed separate.
The fix is the same restraint — solve today's problem simply, reuse what genuinely
fits, and let real (not hypothetical) repetition drive abstraction.
