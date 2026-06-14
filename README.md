# agent-tools

Portable, reusable rules and guards for AI-assisted software development — distilled from
practice across bots, backends, frontends, CLIs, libraries, and multi-agent orchestration,
and generalized so they apply to **any** project, language, or team.

Nothing here is project-specific. Every skill, hook, and template is written to be useful
to a reader with no prior context: the rationale travels, the examples are generic, and the
mechanisms are stack-agnostic (bun/node, python-uv, go).

## What's in here

| Directory      | What it holds                                                                 |
| -------------- | ----------------------------------------------------------------------------- |
| `skills/`      | Advisory rules as markdown skills — `universal/` (any project) and `by-type/{bot,backend,frontend,cli,library,infra,monorepo}/`. Each is one `SKILL.md` with `name` + `description` frontmatter, the portable rule, a rationale, and a generic example. |
| `agent-hooks/` | Programmatic guards that fire on an agent's tool use (the `agents-hooks/v1` contract). Each is a descriptor + executable + README. They enforce, mid-session, what a skill can only advise. |
| `git-hooks/`   | Copyable `pre-commit` / `commit-msg` / `pre-push` / `no-secrets-scan` hooks plus a `lefthook.yml`, generalized across three toolchains. |
| `mcp/`         | Documentation of the multi-model `review` MCP slot (review / quorum / brainstorm / visual) and a code-search MCP slot. |
| `docs/`        | `carrier-decision-guide.md` — when a rule belongs in a skill vs an agent-hook vs a git-hook. |

## The three carriers (and why there are three)

A rule is enforced at a different moment depending on its carrier:

- **Skill** — advice the agent/human reads. The only honest carrier for *judgment* rules
  (naming, design, "investigate before deleting") that no regex captures.
- **Agent-hook** — intercepts a tool call *mid-session* and can block it *before* the side
  effect (block a `--no-verify` bypass; stop a secret being written; prompt the completion
  self-check at end of turn). A git-hook can't do these — it fires too late.
- **Git-hook** — aborts a commit/push for *mechanical* checks (format, types, tests, no
  secrets) — for every committer, human or agent, no harness required.

Many rules want **both** an agent-hook (prevent early) and a git-hook (backstop). See
[`docs/carrier-decision-guide.md`](docs/carrier-decision-guide.md).

## Using the skills

Skills are plain markdown — drop them where your agent harness discovers skills (commonly a
skills directory it scans), or just read them. The frontmatter `description` is a
when-to-use trigger; the body is the rule. They have no runtime and no dependencies.

```
skills/universal/shell-timeouts/SKILL.md
skills/by-type/backend/atomic-db-transactions/SKILL.md
```

## Installing the agent-hooks

Each hook directory has a JSON **descriptor**, an executable **script**, and a **README**.
The descriptor speaks the `agents-hooks/v1` protocol (JSON on stdin, exit `0` = allow,
exit `10` = block, other = error → `on_error` policy). To install one:

1. `chmod +x` the script.
2. Set the descriptor's `cmd` to the script's **absolute path** (the runner rejects
   relative/bare commands).
3. Drop the descriptor into your harness's hook directory for the matching point
   (`pre-bash`, `pre-write`, `stop` — map to your harness's real event names).

See [`agent-hooks/README.md`](agent-hooks/README.md) for the full contract and each hook's
README for test commands.

## Installing the git-hooks

Either copy them into `.git/hooks/` (per clone), point `core.hooksPath` at a tracked
directory, or use [`lefthook`](git-hooks/lefthook.yml) (`lefthook install`) for a committed,
team-wide setup. They auto-detect bun/node vs python-uv vs go. See
[`git-hooks/README.md`](git-hooks/README.md).

## Inventory

- **Universal skills:** 27 — shell-timeouts, exit-codes-through-pipes, dead-code-
  investigation, TDD red-first, test discipline, atomic commits, pre-commit gate, comment &
  naming hygiene, no type escape hatches, systematic debugging, smallest change, shared-util
  single-source, file-header comments, promise = durable action, worktree-base trap, visual
  proof cycle, GAN critic loop, completion self-check, semantic code search, and more.
- **By-type skills:** 45 — bot (12), backend (13), frontend (4), cli (7), library (4),
  infra (1), monorepo (4).
- **Agent-hooks:** 6 — block-no-verify, block-secrets-write, require-review-before-commit,
  enforce-timeout-on-bash, block-raw-process-env, stop-completion-selfcheck.
- **Git-hooks:** 4 templates — pre-commit, commit-msg, pre-push, no-secrets-scan (+ a
  lefthook.yml).
- **MCP:** 1 documented slot (review) + a code-search slot.

## License

MIT — see [LICENSE](LICENSE).
