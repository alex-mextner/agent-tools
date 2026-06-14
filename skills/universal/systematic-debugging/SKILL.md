---
name: systematic-debugging
description: Use when you hit any bug, test failure, or unexpected behavior, before proposing a fix. Reproduce, read the actual error, form one hypothesis, make the smallest change to test it — never stack speculative fixes.
---

# Systematic debugging

Stacking guesses is how a one-line bug becomes a tangle of half-fixes that each
"might help". A disciplined loop is faster and leaves the code cleaner.

## The loop

1. **Reproduce reliably.** A bug you can't trigger on demand can't be confirmed
   fixed. Get a minimal, repeatable reproduction first.
2. **Read the actual error.** The full message and stack trace, not a paraphrase
   from memory. The answer is usually in the text you skimmed.
3. **Compare working vs broken.** What's different between the case that works and
   the one that doesn't — input, state, order, environment? Narrow until you have a
   single suspect.
4. **One hypothesis at a time.** State what you think is wrong and what change would
   prove it. One variable per experiment.
5. **Smallest change that tests the hypothesis.** Not a rewrite — a targeted change
   that confirms or refutes.
6. **Verify against the reproduction.** Re-run the exact failing case. If it's
   green for the right reason, you're done; if not, form the *next* hypothesis from
   what you learned — don't pile a second guess on top of the first.

## Anti-pattern: stacking fixes

Changing five things at once "to be safe" means that if it works you don't know
*why*, and if it doesn't you've added five new variables. When a fix doesn't work,
**revert it** before trying the next idea. Never leave speculative changes lying
around.

## Why

Debugging is a search. Each disciplined step halves the space; each blind guess
doubles it. The loop also produces the thing you need anyway — a reproduction and a
root cause — which is exactly what a regression test encodes (`tdd-red-first`).
