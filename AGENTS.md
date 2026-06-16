# AGENTS.md — agent-tools

This repo is the portable **rule/guard catalog** for AI-assisted development — skills,
agent-hooks, git-hooks, CI gates, MCP slots. It is consumed by
[`rig`](https://github.com/alex-mextner/rig-cli), which reads a committed `rig.yaml` and
wires the catalog into a repo and a dev machine. See `README.md` for the full reference.

If you are an agent working **in** this repo, read the rules below before you act.

> **No always-apply skill list lives here.** Cross-project, mandatory universal skills
> (e.g. `delegate-work-to-subagents`, `visual-proof-cycle`) are surfaced by **rig's
> universal skill layer** — installed by default (`skills.universal.all`) and triggered by
> each skill's own frontmatter `description`. `AGENTS.md` carries **project-specific**
> guidance only; never duplicate a universal skill here. See the "Universal skills vs. a
> project's `AGENTS.md`" section of [`README.md`](README.md).

## Guardrails an agent here must honor

These are the same gates `rig` installs for any repo; they apply here too.

- **Review before commit.** Run `review --staged -C <repo>` (or `review -C <repo>`) in a
  separate step before each commit. Never bypass it for code changes.
- **Atomic, conventional commits.** One logical change per commit; conventional message.
  Never `git --no-verify`. End commit messages with the `Co-Authored-By: Claude` line.
- **Merge only via the ship gate.** `gh ship <PR>` (or `ci/ship/ship.sh`). Never a raw
  `gh pr merge` — it skips the green-CI + resolved-threads + clean-tree gate.
- **Work in a fresh worktree** off the origin default branch, never the main checkout
  (another agent may hold it). Remove your worktree when its branch is merged or pushed.
- **Docs are English-only.** Every agent-facing doc in this repo — `AGENTS.md`, any repo
  `CLAUDE.md`, every `SKILL.md` and `README.md` — is English. No Cyrillic.
