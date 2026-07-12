# Research #263: Two-Day Request Recovery

Date: 2026-07-12
Task: agent-tools #263
Worktree: recovery branch `agent/research-recovery-reports`
Scope: point-in-time recovery report
Sources: Telegram recovery request around tg#7867 and report-publishing follow-up tg#7869

> Snapshot as of 2026-07-12; task states, worktrees, and process rows may have moved since.

## Summary

This pass reconstructed Alex's last two days of requests from task records, local
worktrees, commits, active processes, and spec-web state. The earlier status was too
narrow: it reported that no research processes were active, but did not fully recover the
request queue or publish the Markdown reports.

Three research reports are now available in spec web:

| Report | Status | Spec web |
| --- | --- | --- |
| PM-agent / autonomous manager | Done | https://spec-dev.hyperide.ai/spec/255-pm-agent-research |
| Haft capabilities and provisioning gaps | Done | https://spec-dev.hyperide.ai/spec/2026-07-11-haft-research |
| Thin AST/LSP capability layer | Partial | https://spec-dev.hyperide.ai/spec/256-thin-ast-lsp-layer |

## Project Queue

### agent-tools

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| Recover last two days of requests | task #263 | Doing | Keep this report updated until linked implementation tasks are either closed or explicitly parked. |
| PM-agent research | task #255, PR #259, `docs/research/255-pm-agent-research.md` | Done | Convert recommended next tasks into implementation tickets/specs. |
| Haft research | task #257, PR #258, `docs/specs/2026-07-11-haft-research.md` | Done | Execute provisioning repair plan in rig-cli and stats taxonomy. |
| Thin AST/LSP research | task #256, `docs/research/256-thin-ast-lsp-layer.md` | Partial | Report is complete; close task #256 after review/commit, then create implementation tickets. |
| Non-Claude harness provisioning | tasks #250/#251 | Partial | Finish and verify rig-cli consolidation PR/worktree. |
| Codex orchestration switching | task #254 | Partial | Run required brainstorm/quorum and close the Codex `pre-agent` enforcement gap or record it as unsupported with a concrete fallback. |
| Codex updater rollback | task #252 | Partial | Finish tests, commit, ship, and close the task. |
| Ship CI dedup/pagination | tasks #260/#262 | Partial | Close #260 if PR #261 fully covers it; implement #262 pagination. |

### rig-cli

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| dev-cli permission/allowlist and scripts support | tasks #117/#118, PRs #122/#125 | Done | None for this request. |
| Codex hook bridge provisioning | task #121, PR #124 | Done | None for this request. |
| autonomous mode provisioning | task #130, PR #131 | Done | Verify issue labels/status match the closed work. |
| Consolidate provisioning/updater/orchestration handoffs | task #132, PR #133, dirty worktree `.worktrees/consolidate-250-252-254` | Doing | Run focused tests, review, commit remaining changes, ship PR #133. |

### tg-cli

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| subagent sender label detection | task #159 | Done | None for this request. |
| stdout tg# refs and dense-message warnings/help | task #167, agent-tools #215 | Done | None for this request. |
| private topics / reply handling | PR #180 and task #181 | Partial | PR #180 shipped the base fix; keep task #181 for ask-once fallback behavior. |
| ask once before private-topic fallback | task #181 | Todo | Implement and ship. |
| `/tasks` mobile board | task #178 | Done | Full lifecycle remains separate #115/#117. |
| `/tasks` lifecycle depth | tasks #115/#117 | Todo | Implement origin linkage, acceptance progress, review cycles, accepted-vs-done. |
| Codex usage telemetry collector | task #176 | Todo | Verify real state and either close the label drift or implement collector. |
| `tg-ctl ask` approval primitive | task #149 | Todo | Implement or explicitly rescope against current private-topic work. |

### review-cli / spec web

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| review model presets and Sol seat | task #146, PR #148 | Done | None for this request. |
| `-m` override and unpaid provider handling | PR #140, residual task #139 | Partial | Finish count-side handling for stale/failed seats if still reproducible. |
| spec-web app shell | task #143, PR #145 | Done | None for this request. |
| public spec web report links | `review spec-web add ...`, live daemon on :7920 | Done | Use `https://spec-dev.hyperide.ai/spec/<name>` links in Telegram. |
| spec.dev domain/write origin | task #144 | Blocked | Resolve Cloudflare/domain/cert mismatch or standardize on working `spec-dev.hyperide.ai`. |
| smoke service-lib env mismatch | task #147 | Todo | Fix child CLI env detection. |

### task-cli

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| forgotten task warnings | task #50 | Done | None for this request. |
| warn after priority-changing commands | task #52, dirty worktree `.worktrees/task-52-priority-attention` | Partial | This was forgotten; finish or park the worktree explicitly. |

### research-cli

| Item | Evidence | Status | Next Action |
| --- | --- | --- | --- |
| autonomous research readiness | PM-agent report next tasks | Todo | Install on PATH, replace placeholder AGENTS.md, fix branch drift, record live model composition, support Fable/Sol panel requirement. |

## Active Processes

No active PM-agent, Haft, or AST/LSP research process was found during this recovery pass.
Live support processes are running for review spec-web, review dashboard, tg-ctl, Serena,
Sverklo, and Haft servers. Those are support daemons, not active research jobs.

## Immediate Execution Order

1. Keep the published spec-web links current as reports change.
2. Close or update task #256 after the new AST/LSP report is reviewed and committed.
3. Finish rig-cli consolidation PR #133 covering #250/#251/#252/#254.
4. Implement tg-cli #181 ask-once fallback.
5. Finish task-cli #52 priority-change warning.
6. Resolve review-cli residuals #139/#144/#147.
7. Turn PM-agent and AST/LSP next tasks into implementation tickets.
