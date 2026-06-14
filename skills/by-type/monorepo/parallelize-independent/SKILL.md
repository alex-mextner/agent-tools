---
name: parallelize-independent
description: Use when you have multiple tasks or a new request arrives while other work is in flight. Run independent tasks in parallel; only serialize tasks that share files or have a real dependency. Don't queue a new request behind unrelated in-flight work.
---

# Parallelize independent work; serialize only real dependencies

The default for multiple pieces of work should be **parallel**. Serializing — doing them
one after another — is a cost you pay only when there's an actual reason: a shared file
they'd both edit, or a genuine dependency where one needs the other's output.

## Rule

- **Independent tasks → parallel.** Two bug fixes in different modules, three subagents on
  three unrelated features, a research task and a build task — these have no reason to wait
  on each other. Dispatch them concurrently (each in its own worktree — see
  `worktree-isolation`).
- **Don't block a new request behind unrelated in-flight work.** When a fresh request
  arrives while something else is running, ask: does it *depend* on the in-flight work or
  *touch the same files*? If not, start it now in parallel — don't make it wait its turn
  for no reason.
- **Serialize only when:**
  - the tasks edit the **same files** (parallel edits would conflict), or
  - one **genuinely depends** on another's result (B needs A's output).

```
3 unrelated bug fixes        → 3 parallel subagents, 3 worktrees
"fix parser" + "add a flag to the same file" → serialize (shared file)
"build" then "deploy the build"             → serialize (real dependency)
```

## Why

Serializing independent work wastes the biggest advantage of multiple agents — they can
actually run at once. The common mistake is reflexively queuing everything, which turns a
parallelizable batch into a slow sequence for no benefit. The discipline is to *check* for
the two real reasons to serialize (shared files, true dependency) and parallelize
everything else. Pairs with `subagent-handoff-contract` (so parallel results compose) and
`worktree-isolation` (so parallel edits don't collide).
