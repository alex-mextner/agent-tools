# Leftover-marker gate

Fail a PR on the classic "oops, left it in" leftovers — scanned on the lines the PR
**adds**, so pre-existing debt doesn't block you.

| Rule | Matches | Why it's a leftover |
| ---- | ------- | ------------------- |
| `focused-test` | `.only(`, `fdescribe`, `fit(` | Silently skips the rest of the suite — your CI goes green while testing almost nothing. |
| `debugger` | `debugger;` | A breakpoint that ships freezes a real browser session. |
| `console` | `console.log` / `console.debug` | Debug noise in production logs (block by default; `ALLOW_CONSOLE=1` to warn). |
| `untracked-todo` | `TODO`/`FIXME` with **no** issue ref | A TODO with no ticket is a TODO that never gets done. `TODO(ABC-123)` / `TODO #45` / a URL passes. |
| `merge-marker` | `<<<<<<<` / `=======` / `>>>>>>>` | A botched merge committed raw. |

## Quick start

```bash
cp ci/leftover-grep/workflow.yml .github/workflows/leftover-grep.yml
# The workflow runs `bash ci/leftover-grep/leftover-grep.sh`, so the script must be present
# at that path — vendor the ci/ dir, or copy the script and adjust the run: path:
cp ci/leftover-grep/leftover-grep.sh .github/scripts/leftover-grep.sh
# Local pre-push:
sh ci/leftover-grep/leftover-grep.sh
```

## Knobs

- `LEFTOVER_INCLUDE` / `LEFTOVER_EXCLUDE` — ERE of paths to scan / skip. Defaults cover
  common source extensions and exclude `node_modules`, build dirs, lockfiles, snapshots.
- `TICKET_REGEX` — what makes a TODO "tracked". Default accepts `ABC-123`, `#123`, or a URL.
  Set it to your tracker's id format to be strict (e.g. `JIRA-[0-9]+`).
- `ALLOW_CONSOLE=1` — downgrade `console.log` to a warning (some projects log intentionally;
  scope `LEFTOVER_INCLUDE` to app code if so).
- `LEFTOVER_BASE` — diff base (default `origin/main` → `main` → full-tree).
- `LEFTOVER_HEAD` — head ref/SHA to diff against the base (default `HEAD`). The
  `pull_request_target` workflow sets this to the PR head SHA, fetched as **data** — see
  below.
- `LEFTOVER_FULLTREE=1` — scan the whole tree, not just the diff (for a one-off cleanup).

## Tamper-resistant in CI (`pull_request_target`)

A merge-blocking gate must not run a script the PR can edit. This gate also needs the PR's
*code* — but only to **read** it. The workflow uses `pull_request_target` to run the
**base-branch (trusted)** script, fetches the PR head commit as **data**, and diffs+greps it
(`LEFTOVER_HEAD=<pr-head-sha>`). `git diff` never executes the PR code. **Hard rule:** do not
add a build/test/install step to that workflow — that would execute PR code under the
privileged trigger.

## Diff-scoped by default

It parses `git diff` and inspects only **added** lines. That's deliberate: a repo with
existing `console.log`s shouldn't block every new PR — only new leftovers fail. Use
`LEFTOVER_FULLTREE=1` when you want to clean up the whole codebase at once.

## Escape hatch

There's no inline-suppress comment by design (a leftover you "suppress" is a leftover you
keep). If a match is a legitimate false positive, narrow `LEFTOVER_INCLUDE`/`EXCLUDE` or the
specific rule's regex in the script. For an intentional `console.log` in app logging, set
`ALLOW_CONSOLE=1` and scope the include path.

## When to use

Every repo with tests and source. It's cheap, fast, dependency-free, and catches a
genuinely embarrassing class of mistake. Pairs with the `test-discipline` and
`deferred-findings-tracking` skills (the latter is the "TODO needs a ticket" rationale).
