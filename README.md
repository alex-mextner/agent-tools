# agent-tools

`agent-tools` is the portable **rule/guard catalog** for AI-assisted development — skills,
agent-hooks, git-hooks, CI gates, and MCP slots, distilled from practice and generalized so
they apply to **any** project, language, or team. It is a *library*, not an installer: you
don't cherry-pick 36 skills, 8 hooks, and 12 CI gates by hand. You install the whole catalog
through one front door — [`rig`](https://github.com/alex-mextner/rig-cli) — which reads a
committed `rig.yaml` and wires it in. agent-tools is the **what**; rig is the **how**.

## Quick start — the single front door (`rig init`)

You enter agent-tools through `rig`. One command, run once, scaffolds a `rig.yaml`, wires in
the catalog, and recommends auto-mode by default:

```bash
rig init          # first-run onboarding: scaffold rig.yaml + wire the catalog in
rig apply         # steady state: re-apply on every machine, identically (idempotent, backs up on conflict)
rig status        # later: has the repo drifted from rig.yaml?
```

`rig init` and `rig apply` are two distinct commands: `init` is first-run onboarding (no
config yet → scaffold one and walk you through the catalog); `apply` is the steady-state
reconcile (converge the disk to an existing `rig.yaml`). Interactivity (full TUI / semi /
non-interactive `--yes`) is orthogonal — both run in any mode. (`rig setup` is a back-compat
alias of `rig init`.) rig scans this catalog live and installs what `rig.yaml` enables
— skills to your harness's skills dir, agent-hooks to its hook dir, the global git-hook
dispatcher, CI workflows, MCP registrations. Update agent-tools, re-run `rig apply`, and the
new items flow in with no code change in rig. See
[rig-cli](https://github.com/alex-mextner/rig-cli) for install and the full `rig.yaml`
schema.

## Why this exists: autonomous, observable, controllable — and safe

The point of wiring the whole catalog in one shot is to let an agent **work autonomously
with minimum babysitting** after a single front-door command. That is only sane because of
the guards. The spine:

> **The guards make auto-mode safe; the logging makes it observable; the config makes it
> controllable.**

| Pillar | Mechanism in agent-tools |
| --- | --- |
| **Autonomous** | **Auto-mode** — the harness auto-accepts tool calls, so the agent runs without a permission prompt on every step. rig provisions this (`harness.auto_mode`) and **recommends it on by default** — *because the guards below are installed.* |
| **Safe** | **Agent-hooks** intercept the dangerous call *before* the side effect: block a secret write, block a `--no-verify` gate bypass, enforce shell timeouts, block a raw `process.env` read, and **block a raw `gh pr merge` that skips the green-CI + screenshot ship gates** (`block-raw-pr-merge`). The guards are what *buy* the autonomy — auto-mode is only safe because the irreversible actions are caught mid-session. |
| **Observable** | **Structured JSONL logging** (`lib/agenttools_log`) — one log shape across the ecosystem CLIs, plus tg reporting — so you can see what the agent actually did. |
| **Controllable** | **Config** (`rig.yaml`) is the steering wheel: enable/disable any item, pin targets, set escape hatches. Paired with decisions-as-buttons / `tg ask`, you steer the agent without sitting on it. |

Every guard ships an **escape hatch** (an env var or an inline sentinel), so safety is a
controllable gate, not a hard wall — you can override a guard *with an explicit, logged
reason* when you genuinely need to.

---

# Reference

Everything below is the catalog reference — the carriers, the per-directory map, install
instructions, and the inventory.

Nothing here is project-specific. Every skill, hook, and template is written to be useful to
a reader with no prior context: the rationale travels, the examples are generic, and the
mechanisms are stack-agnostic (bun/node, python-uv, go).

## What's in here

| Directory      | What it holds                                                                 |
| -------------- | ----------------------------------------------------------------------------- |
| `skills/`      | Advisory rules as markdown skills — `universal/` (any project) and `by-type/{bot,backend,frontend,cli,library,infra,monorepo}/`. Each is one `SKILL.md` with `name` + `description` frontmatter, the portable rule, a rationale, and a generic example. |
| `agent-hooks/` | Programmatic guards that fire on an agent's tool use (the `agents-hooks/v1` contract). Each is a descriptor + executable + README. They enforce, mid-session, what a skill can only advise. |
| `git-hooks/`   | Copyable `pre-commit` / `commit-msg` / `pre-push` / `no-secrets-scan` hooks plus a `lefthook.yml`, generalized across three toolchains — and a **global dispatcher** (`global-dispatcher/`) that runs every hook in `~/.config/git/global-hooks.d/` in **every** repo, even ones whose lefthook/husky override `core.hooksPath`. |
| `ci/`          | Drop-in CI / PR-gate building blocks a CI-building agent looks for first — one slot per concern (workflow + optional shell script + README): secret-scan (gitleaks), CodeQL (incl. a no-GHAS self-gate), semgrep SAST, dependency-review + license, AI-review, Copilot-findings, unresolved-review-thread block, unchecked-checkbox block, mandatory screenshots, conventional-commit PR-title lint, leftover-marker grep, and a green-CI-gated `ship` merge command. See [`ci/README.md`](ci/README.md). |
| `mcp/`         | The MCP-vs-CLI+skill policy (why `review` is a CLI+skill, not an MCP) and a code-search MCP slot. |
| `lib/`         | Reusable, importable library code + shared contracts the ecosystem CLIs depend on. `agenttools_log` — shared **structured JSONL logging** (stdlib-only) so `review-cli`, `rig-cli`, and future Python CLIs log in one shape. `contracts/models.yaml` — the **model board**: the current-best concrete model per provider, each tagged with capabilities (esp. `vision`, for the image-review filter), plus a symbolic roles/aliases map (validated by `contracts/models.schema.json`). `checker/model_freshness.py` — a **daily currency checker** that polls provider model-list endpoints and PROPOSES version bumps (a PR, or a dated report) when a newer model appears (semi-automatic; a human confirms). rig provisions the checker as a daily noon cron. See [`lib/README.md`](lib/README.md), [`lib/checker/README.md`](lib/checker/README.md). |
| `docs/`        | `carrier-decision-guide.md` — when a rule belongs in a skill vs an agent-hook vs a git-hook. |

## The three carriers (and why there are three)

A rule is enforced at a different moment depending on its carrier:

- **Skill** — advice the agent/human reads. The only honest carrier for *judgment* rules
  (naming, design, "investigate before deleting") that no regex captures.
- **Agent-hook** — intercepts a tool call *mid-session* and can block it *before* the side
  effect (block a `--no-verify` bypass; stop a secret being written; block a raw `gh pr
  merge` that skips the ship gates; prompt the completion self-check at end of turn). A
  git-hook can't do these — it fires too late.
- **Git-hook** — aborts a commit/push for *mechanical* checks (format, types, tests, no
  secrets) — for every committer, human or agent, no harness required.

Many rules want **both** an agent-hook (prevent early) and a git-hook (backstop). See
[`docs/carrier-decision-guide.md`](docs/carrier-decision-guide.md).

## Using the skills

Skills are plain markdown — drop them where your agent harness discovers skills (commonly a
skills directory it scans), or just read them. The frontmatter `description` is a
when-to-use trigger; the body is the rule. They have no runtime and no dependencies.

> **Two of these are always-apply — not opt-in, every task, every agent:**
> [`delegate-work-to-subagents`](skills/universal/delegate-work-to-subagents/SKILL.md) (the
> main thread is an orchestrator — plan, decompose, dispatch to subagents or a dynamic
> workflow, and verify; it does **not** do non-trivial work inline) and
> [`visual-proof-cycle`](skills/universal/visual-proof-cycle/SKILL.md) (capture and **look
> at** any user-visible change before claiming it works). Both are universal skills, so rig
> installs them by default (`skills.universal.all`), and their strong frontmatter triggers
> surface them on any matching task. See the **Always-apply skills** section of
> [`AGENTS.md`](AGENTS.md) for the mandate.

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

### Universal skills vs. a project's `AGENTS.md`

There are two different homes for agent guidance, and they must not be confused:

- **A project's `AGENTS.md` (or a repo-level `CLAUDE.md`) is for project-specific guidance
  only** — how *this* repo builds, its layout, its local conventions. Nothing in it should
  apply to every other repo.
- **Cross-project, always-apply MANDATORY skills** (for example `visual-proof-cycle` or
  `task-completion-selfcheck`) live here in `skills/universal/`. They are provisioned by
  **rig's universal skill layer** and meant to reach **every project and every user** through
  the SessionStart blurb, the rig-installed skills, and each skill's own trigger
  `description`. That layer is their single source of truth.

**Never copy a universal mandatory skill into an individual `AGENTS.md`.** Duplicating it
there pins a stale copy to one repo, hides the real source, and goes stale the moment the
skill changes. The universal layer is the one place that carries these mandates across all
projects and users — let it. `AGENTS.md` stays project-specific; universal mandates stay
universal.

## Installing the agent-hooks

Each hook directory has a JSON **descriptor**, an executable **script**, and a **README**.
The descriptor speaks the `agents-hooks/v1` protocol (JSON on stdin, exit `0` = allow,
exit `10` = block, other = error → `on_error` policy). To install one:

1. `chmod +x` the script.
2. Set the descriptor's `cmd` to the script's **absolute path** (the runner rejects
   relative/bare commands).
3. Drop the descriptor into your harness's hook directory for the matching point
   (`pre-bash`, `pre-write`, `post-write`, `stop` — map to your harness's real event names).

`rig apply` does all three for you (it rewrites the `cmd` placeholder to the script's
absolute path in your agent-tools checkout). See [`agent-hooks/README.md`](agent-hooks/README.md)
for the full contract and each hook's README for test commands.

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

- **Universal skills:** 37 — shell-timeouts, exit-codes-through-pipes, dead-code-
  investigation, TDD red-first, test discipline, atomic commits, pre-commit gate, secret
  scanning (gitleaks, hook + CI), CI gate suite, global git-hooks dispatcher, git-workflow
  safety (reset/fixup/partial-staging/worktree-removal), comment &
  naming hygiene, no type escape hatches, systematic debugging, smallest change, shared-util
  single-source, file-header comments, promise = durable action, worktree-base trap,
  **delegate-work-to-subagents** (the main thread orchestrates — plan, dispatch to
  subagents, verify — never does non-trivial work inline) and **visual proof cycle** (look
  at any user-visible change before claiming it works) — the two **always-apply** skills
  (see [`AGENTS.md`](AGENTS.md)) — GAN critic loop, completion self-check, semantic code
  search, **web-page reading via agent-browser** (read full pages/docs with the
  `agent-browser` CLI instead of a truncating fetch tool), and more.
- **By-type skills:** 45 — bot (12), backend (13), frontend (4), cli (7), library (4),
  infra (1), monorepo (4).
- **Agent-hooks:** 8 — block-no-verify, block-raw-pr-merge, block-secrets-write,
  require-review-before-commit, enforce-timeout-on-bash, block-raw-process-env,
  stop-completion-selfcheck, **format-on-write** (runs the project's configured formatter
  on each file the agent writes — oxfmt/prettier/biome/ruff/black/gofmt/rustfmt; never
  blocks).
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
- **MCP:** a code-search slot (`review` is a CLI+skill, not an MCP — see [`mcp/README.md`](mcp/README.md)).

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
- **[review-cli](https://github.com/alex-mextner/review-cli)** — multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — umbrella dev-env driver: sets up a repo from config — skills, hooks, CI, dep-bootstrap; reconciles drift
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.

## License

MIT — see [LICENSE](LICENSE).
