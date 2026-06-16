# require-ticket-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 25 · **Default:** warn (advisory)

On a `git commit`, checks the commit message and branch name for a reference to a
tracking ticket. If none is found — and the commit isn't an exempt chore/WIP/merge —
it **warns** (default) or, in strict mode, **blocks**, with a reminder that non-trivial
changes should start from a ticket with acceptance criteria, motivation, and
user-impact. Enforces the `strict-ticket-discipline` skill; pairs with task-cli.

## Ticket-detection heuristic

Detection is intentionally **broad**: a missed reference at worst nags (the default is
warn / fail-open), while a false positive only lets through a commit that almost
certainly had a ticket anyway. It scans the commit message *and* the branch name
(ticket ids are often encoded as `feature/ABC-12-foo`) for:

| Form                          | Example                                  | Tracker        |
| ----------------------------- | ---------------------------------------- | -------------- |
| `#NNN`                        | `Refs #123`                              | GitHub issue/PR |
| `org/repo#NNN`                | `alex-mextner/task-cli#4`                | GitHub (qualified) |
| `GH-NNN`                      | `GH-123`                                 | GitHub         |
| `KEY-NNN` (≥2 uppercase)      | `ENG-456`, `ABC-12`                      | Linear / Jira / task-cli |
| `task:…` / `task #…` / `T-NN` | `task:ABC-12`, `T-9`                     | task-cli       |
| trailer keyword + ref         | `Closes #12`, `Fixes ABC-3`, `Refs: T-9` | any            |
| full tracker URL              | `github.com/o/r/issues/12`, `linear.app/.../issue/…` | any |

The message is pulled from the argv (`-m`/`--message`, `-mMsg`, and the contents of any
`-F`/`--file` commit-message file), so a ticket id in any of those counts. A relative
`-F` path is resolved against the event's `cwd` (the command's working directory), so it
works even when the harness launches the hook outside the repo. A trailer keyword
(`Closes`/`Fixes`/`Refs`) only counts when it's followed by a real ref — `fix: null deref`
is a bugfix subject, not a reference, so in strict mode it correctly still needs a ticket.

## Exemptions (no ticket expected)

These commits skip the gate by default:

- conventional-commit chore types: `chore:`, `docs:`, `style:`, `ci:`, `build:`,
  `test:`, `revert:`
- WIP / fixup markers: `wip…`, `fixup!`, `squash!`, `amend!`, `merge…`, `revert…`
- `git commit --amend/--continue/--abort/--skip` (not authoring a fresh change)

## Configure (env)

- `REQUIRE_TICKET_STRICT=1` — turn a missing ticket into a hard **block** (default: warn).
- `REQUIRE_TICKET_EXEMPT_TYPES="chore,docs"` — override the exempt commit-type set
  (comma-separated; replaces the default list).

## Why an agent-hook

The check has to happen *before* the commit, mid-session, while you can still add the
reference. It's also why this is fail-open and warn-by-default: it's process discipline,
not a security boundary, so a crash or a heuristic miss must never make committing
impossible. (Contrast `block-no-verify` / `block-secrets-write`, which are fail-closed.)
For a hard CI backstop, pair with an optional `ci/ticket-required` gate.

## Install

```bash
chmod +x require_ticket_before_commit.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory.
```

## Test

```bash
chmod +x require_ticket_before_commit.py

# no ticket → warns but allows (default)
echo '{"args":{"command":"git commit -m \"feat: add export\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow","message":"No ticket reference ..."}  exit=0

# ticket present → clean allow
echo '{"args":{"command":"git commit -m \"feat: add export (Refs #123)\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

# strict mode, no ticket → block (exit 10)
REQUIRE_TICKET_STRICT=1 sh -c 'echo "{\"args\":{\"command\":\"git commit -m x\"}}" | ./require_ticket_before_commit.py'; echo " exit=$?"
# → decision":"block ...  exit=10

# exempt chore type → clean allow even without a ticket
echo '{"args":{"command":"git commit -m \"chore: bump lockfile\""}}' | ./require_ticket_before_commit.py; echo " exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Run the unit tests with `python3 test_require_ticket.py` (stdlib `unittest`, no deps).
