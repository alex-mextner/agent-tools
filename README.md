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
| `git-hooks/`   | Copyable `pre-commit` / `commit-msg` / `pre-push` / `no-secrets-scan` hooks plus a `lefthook.yml`, generalized across three toolchains — and a **global dispatcher** (`global-dispatcher/`) that runs every hook in `~/.config/git/global-hooks.d/` in **every** repo, even ones whose lefthook/husky override `core.hooksPath`. |
| `ci/`          | Drop-in CI / PR-gate building blocks a CI-building agent looks for first — one slot per concern (workflow + optional shell script + README): secret-scan (gitleaks), CodeQL (incl. a no-GHAS self-gate), semgrep SAST, dependency-review + license, AI-review, Copilot-findings, unresolved-review-thread block, unchecked-checkbox block, mandatory screenshots, conventional-commit PR-title lint, leftover-marker grep, and a green-CI-gated `ship` merge command. See [`ci/README.md`](ci/README.md). |
| `mcp/`         | Documentation of the multi-model `review` MCP slot (review / quorum / brainstorm / visual) and a code-search MCP slot. |
| `lib/`         | Reusable, importable library code the ecosystem CLIs depend on. Currently `agenttools_log` — shared **structured JSONL logging** (stdlib-only) so `review-cli`, `rig-cli`, and future Python CLIs log in one shape. See [`lib/README.md`](lib/README.md). |
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
skills/universal/web-page-reading-agent-browser/SKILL.md
skills/by-type/backend/atomic-db-transactions/SKILL.md
```

> **Reading a web page, API doc, or reference?** Reach for the
> [`web-page-reading-agent-browser`](skills/universal/web-page-reading-agent-browser/SKILL.md)
> skill: drive the [`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI
> (`open` + `get text body` / `eval`) instead of a fetch-and-summarize tool that
> **truncates long pages and silently drops content**. It loads the full JS-rendered page
> and lets you extract just the section you need — for very long pages, extract a bounded
> slice (scoped selector / `eval`) or save to a file, so your own tool's output limit
> doesn't re-truncate it. `agent-browser` is a standalone
> third-party CLI (`npm i -g agent-browser && agent-browser install`, or `cargo install
> agent-browser && agent-browser install`) — not bundled here; the skill is the advisory
> "reach for it, and how."

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
team-wide setup. They auto-detect bun/node vs python-uv vs go. To make a hook run in **every**
repo on the machine — even ones with lefthook/husky that shadow `core.hooksPath` — use the
**global dispatcher** in [`git-hooks/global-dispatcher/`](git-hooks/global-dispatcher/README.md)
(`install-local-hooks.sh` retrofits an existing repo; `hooks-sweep` does the whole machine).
See [`git-hooks/README.md`](git-hooks/README.md) and the `global-git-hooks` skill.

## Using the CI tools

`ci/` holds drop-in CI / PR-gate building blocks — the first place a CI-building agent
should look for "is there a standard way to do this in CI?" It covers secret scanning
(gitleaks), SAST (CodeQL incl. a no-GHAS self-gate, and semgrep), dependency + license
review, an AI-review step, surfacing Copilot findings, and the "block merge until X" gates
(unresolved review comments, unchecked PR checkboxes, missing screenshots, non-conventional
PR titles, debug leftovers in the diff), plus a green-CI-gated `ship` merge command. Each
slot is a workflow (+ usually a generic shell script for non-GitHub CI) and a README with
the standard engine, knobs, and escape hatch. Copy the workflow into `.github/workflows/`,
or call the shell script from a pipeline step. Start at [`ci/README.md`](ci/README.md) and
the [`ci-gate-suite`](skills/universal/ci-gate-suite/SKILL.md) skill.

## Inventory

- **Universal skills:** 31 — shell-timeouts, exit-codes-through-pipes, dead-code-
  investigation, TDD red-first, test discipline, atomic commits, pre-commit gate, secret
  scanning (gitleaks, hook + CI), CI gate suite, global git-hooks dispatcher, comment &
  naming hygiene, no type escape hatches, systematic debugging, smallest change, shared-util
  single-source, file-header comments, promise = durable action, worktree-base trap, visual
  proof cycle, GAN critic loop, completion self-check, semantic code search, **web-page
  reading via agent-browser** (read full pages/docs with the `agent-browser` CLI instead of
  a truncating fetch tool), and more.
- **By-type skills:** 45 — bot (12), backend (13), frontend (4), cli (7), library (4),
  infra (1), monorepo (4).
- **Agent-hooks:** 6 — block-no-verify, block-secrets-write, require-review-before-commit,
  enforce-timeout-on-bash, block-raw-process-env, stop-completion-selfcheck.
- **Git-hooks:** 4 templates — pre-commit, commit-msg, pre-push, no-secrets-scan (+ a
  lefthook.yml) — plus a global dispatcher (`global-dispatcher/`: `run-global-hooks`,
  `install-local-hooks.sh`, `hooks-sweep`, and a `global-hooks.d/` drop-in tree) that runs
  every global hook in every repo regardless of lefthook/husky `core.hooksPath` overrides.
- **CI:** 12 slots — secret-scan (gitleaks), codeql (incl. private-repo self-gate), sast
  (semgrep), dependency-review (+ license + multi-ecosystem audit), ai-review (configurable
  reviewer), copilot-findings (surface + optional gate), review-threads (unresolved-comment
  block), pr-checklist (unchecked-checkbox block), screenshots (mandatory image on UI PRs),
  pr-title-lint (Conventional Commits), leftover-grep (no `.only`/`debugger`/`console`/
  untracked-TODO), ship (green-CI-gated merge + cleanup). Plus a backlog of net-new ideas in
  `ci/README.md` (coverage-delta, bundle-size, doc-link-check, CODEOWNERS, stale-PR,
  size-label).
- **MCP:** 1 documented slot (review) + a code-search slot.

## How agent-tools compares

The closest neighbours each cover *one* carrier. **Git-hook managers** (pre-commit,
lefthook, husky) install commit/push-time checks — and nothing fires earlier or later than
that. **Curated skill lists** (awesome-claude-code and the many `everything-claude-code` /
`.claude-templates` collections) are link directories of prompts and configs, almost always
Claude-Code-specific, with no enforcement at all.

`agent-tools` spans all three carriers — **skills** (judgment rules an agent reads),
**agent-hooks** (block a tool call *mid-session*, before the side effect), and **git-hooks**
(mechanical backstop at commit/push) — plus **CI gates** and **MCP slots**, and a shared
`agenttools_log` lib. Everything is **harness-agnostic** (not tied to one agent) and
**generic** (no project assumptions; the rationale travels).

| Project | Git hooks | Agent-hooks (mid-session block) | Advisory skills | CI gates | MCP slots | Harness-agnostic | Generic / portable |
|---|---|---|---|---|---|---|---|
| **agent-tools** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| pre-commit | ✓ | — | — | ~ (CI action) | — | n/a | ✓ |
| lefthook | ✓ | — | — | ~ | — | n/a | ✓ |
| husky | ✓ | — | — | — | — | n/a | ~ (Node-centric) |
| awesome-claude-code (+ similar lists) | ~ (links) | ~ (links) | ✓ (links) | — | ~ (links) | — (Claude-only) | ~ (curated, not generalized) |

`~` = partial, `n/a` = not an agent tool. Hook managers are excellent at the commit gate
but cannot stop a `--no-verify` bypass or a secret being written *mid-session* — that needs
an agent-hook. Skill lists supply ideas but no mechanism. `agent-tools` is the only one here
that carries advice, mid-session enforcement, and the commit/CI backstop together, and is
consumed declaratively by [`rig`](https://github.com/alex-mextner/rig-cli).

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[review-cli](https://github.com/alex-mextner/review-cli)** — agentic, priority-ordered failover multi-model code-review board (brainstorm/quorum, spec-web, dashboard)
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — umbrella dev-env driver: sets up a repo from config — skills, hooks, CI, dep-bootstrap; reconciles drift
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.

## License

MIT — see [LICENSE](LICENSE).
