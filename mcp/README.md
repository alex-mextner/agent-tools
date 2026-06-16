# MCP servers — when (not) to add one

**Default: don't.** Our tools advertise themselves to agents as a **CLI + a skill**
(`<tool> install-skill` drops a `SKILL.md` that teaches the agent the command); the agent
reads the skill and runs the binary in a shell. That is the right shape for almost every dev
tool — ~zero idle context (the skill body loads only on trigger), no second process to drift
out of sync, and it works in every shell-having host (Claude Code, Codex, Cursor).

An **MCP server** is the adapter for a system the agent *cannot reach from a shell*. Add one
here ONLY if you can tick at least one box:

- [ ] **No shell on the host** — a non-CLI agent host (web app, IDE sandbox) that cannot `exec` a binary.
- [ ] **Stateful / long-lived session** — holds a connection, cursor, REPL, index, or subscription across calls (debugger, DB session, live browser, code index).
- [ ] **Streaming / incremental results** the agent consumes as they arrive.
- [ ] **No CLI exists** for the underlying system (a remote API / SaaS / index with no binary entry point).
- [ ] **Enterprise governance** — centralized OAuth, per-user auth, RBAC, or audit across a multi-tenant boundary.
- [ ] **Dynamic tool discovery** — the agent must enumerate capabilities at runtime.

If none apply, ship a **CLI + skill** instead. An MCP wrapper around your own local CLI pays a
permanent context tax (MCP loads every registered tool's schema up front — easily tens of
thousands of tokens before the agent acts), adds a process + registration that drifts out of
sync (the classic dead-`--mcp`-flag failure), and degrades tool-selection accuracy as the tool
count climbs. It buys nothing the skill does not already give.

## What belongs here

### Code search (serena / sverklo / language-server style) — JUSTIFIED ✓

An index-backed code-search server (find-symbol / find-references over a built index) is a
real MCP case: it is **stateful** (holds the index), there is usually **no shell CLI** for it,
and the agent benefits from structured symbol results. The `semantic-code-search` skill
recommends preferring such a server over `grep` + whole-file reads, falling back to grep only
for literal-string searches or unindexed repos. These servers (serena, sverklo, …) are
third-party — register them in your harness's MCP config per their own docs.

## What was removed

### `review/` — NOT an MCP (it is a CLI + skill)

`review` (multi-model code review) is the textbook CLI+skill case: read-only, stateless,
run-and-return, prints markdown, already shell-callable, already ships a skill. It ticks
**zero** boxes above. Its minutes-long runtime is an argument *against* MCP — a long batch job
wants a backgroundable shell call, not a blocking MCP tool call. So review is a CLI + skill;
its MCP slot (and the dead `review --mcp` registration) was removed. The "executable rules" it
backed — `ai-review-before-commit`, `gan-critic-loop`, `visual-proof-cycle` — are delivered by
the **CLI's exit codes + `--json` + the agent-hooks**, not by MCP transport. If we ever build a
**shell-less** review consumer (a pure web / IDE-sandbox host with no `exec`), that is the
moment to add `mcp/review/` back — and only then.

---

Research basis (2025-2026 consensus — CLI + Skills for the local dev inner loop, MCP for
systems-without-a-shell): Mario Zechner's MCP-vs-CLI benchmark (120 runs), Anthropic
"Equipping agents for the real world with Agent Skills", Milvus "Is MCP Dead?".
