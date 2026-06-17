# AGENTS.md — agent-tools

This repo is the portable **rule/guard catalog** for AI-assisted development — skills,
agent-hooks, git-hooks, CI gates, MCP slots. It is consumed by
[`rig`](https://github.com/alex-mextner/rig-cli), which reads a committed `rig.yaml` and
wires the catalog into a repo and a dev machine. See `README.md` for the full reference.

If you are an agent working **in** this repo, read the rules below before you act.

## Always-apply skills (mandatory — every task, every agent)

These two skills are **not** opt-in and **not** task-specific. They apply to the main
orchestrator thread and to every subagent, on every task. They exist because the two most
common, most expensive agent failures are (a) the main thread doing non-trivial work inline
instead of delegating, and (b) claiming a user-visible change is "done" without ever looking
at the rendered result. Internalize both before you touch the work.

| Skill | Trigger — when it applies | The rule, in one line |
| --- | --- | --- |
| [`delegate-work-to-subagents`](skills/universal/delegate-work-to-subagents/SKILL.md) | Any task beyond a trivial one-liner — multi-step coding, research, or any repo mutation. | The main thread is an **orchestrator**: plan, decompose, dispatch to subagents (the `Agent` tool) or a dynamic workflow, and **verify** their results. Do not implement inline. |
| [`visual-proof-cycle`](skills/universal/visual-proof-cycle/SKILL.md) | Any user-visible change — UI, a rendered image, a chart, generated output. | Capture the rendered result, **look at it yourself**, review critically, fix, re-capture. "It builds" is not "it works"; attach the final capture as evidence. |

Two non-negotiable consequences:

- **Do not do non-trivial work inline.** The moment a task grows past a one-liner, it is a
  subagent or a dynamic workflow — not the orchestrator's own edits. See
  `delegate-work-to-subagents`.
- **Do not claim a user-visible change is done without looking at it.** Capture the rendered
  output, inspect the capture yourself, and attach it. See `visual-proof-cycle`.

Both are **universal** skills, so `rig` installs them by default on every machine: the
global rig config selects `skills.universal.all: true` (default-on; a skill is included
unless explicitly disabled), which carries every `skills/universal/*` skill — these two
included — into the harness's skills dir. Their strong, always-on frontmatter
`description` (the when-to-use trigger) is what makes the harness surface them on any
matching task; this AGENTS.md is the per-session backstop that states they are mandatory,
not optional.

## Other guardrails an agent here must honor

These are the same gates `rig` installs for any repo; they apply here too.

- **Review before commit.** Run `review --staged -C <repo>` (or `review -C <repo>`) in a
  separate step before each commit. Never bypass it for code changes.
- **Atomic, conventional commits.** One logical change per commit; conventional message.
  Never `git --no-verify`. End commit messages with the `Co-Authored-By: Claude` line.
- **Merge ONLY via `gh ship <PR>`.** That is the single sanctioned merge interface (a gh
  alias → the repo's provisioned `pr-ship.sh`). Do NOT invoke `ship.sh` / `pr-ship.sh` by
  path, do NOT invent or hand-roll a merge script, do NOT `gh pr merge` — every alternative
  skips the green-CI + resolved-threads + clean-tree gate. Flags pass straight through:
  `gh ship 27`, `gh ship 36 --skip-ci`, `gh ship 33 --screenshot ./after.png "badge"`. If
  `gh ship` is missing in a repo, fix the provisioning (`rig apply`) — never route around it
  with the raw script.
- **Work in a fresh worktree** off the origin default branch, never the main checkout
  (another agent may hold it). Remove your worktree when its branch is merged or pushed.
- **Docs are English-only.** Every agent-facing doc in this repo — `AGENTS.md`, any repo
  `CLAUDE.md`, every `SKILL.md` and `README.md` — is English. No Cyrillic.
