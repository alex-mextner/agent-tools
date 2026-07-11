# Research #255: Autonomous PM-Agent For The Task Ecosystem

Date: 2026-07-11
Worktree: `/Users/ultra/xp/.worktrees/agent-tools-255-pm-agent`
Task: agent-tools #255

## Scope And Approval

Alex approved autonomous parallel research while offline in the task handoff. That satisfies
the task's approval gate for research and external brainstorm usage. This pass did not
implement product code.

## Local Evidence Inspected

### Task Records

- agent-tools #255: requested PM-agent research and acceptance criteria.
- agent-tools #222: active backlog drain work; demonstrates need for a unified queue that
  lists active projects, tickets, owners, status, and next action.
- agent-tools #254: Codex context switching problem; PM-agent must preserve independent
  work instead of serializing unrelated requests into the current pane.
- agent-tools #250/#251: non-Claude harness provisioning investigation; shows harness
  capability and provisioning are first-class PM inputs.
- agent-tools #226 and tg-cli #171: transient provider overloads now auto-continue with
  increasing backoff; PM-agent should consume this as an executor-health event.
- agent-tools #227 and tg-cli #172: Codex hard usage-limit diagnostics now distinguish
  missing/stale/below-threshold/shadowed telemetry; PM-agent should route work before
  hard stops.
- agent-tools #205: stall watchdog design; PM-agent should treat WARN/ABORT as lifecycle
  events, not ad-hoc alerts.
- agent-tools #163: orchestrator-only and worktree-only enforcement exists as hook policy;
  PM-agent must honor it and delegate implementation.
- agent-tools #186: rig apply drift is not automatically surfaced after hook changes;
  PM-agent should check rig drift/config health proactively.
- task-cli #50: stale session warnings already exist, but only as a read-only warning layer.
  This is useful evidence that "forgotten work" is a known gap, not a hypothetical.

### Repositories And Files

- task-cli:
  - `/Users/ultra/xp/task-cli/AGENTS.md`
  - `/Users/ultra/xp/task-cli/tasklib/model.py`
  - `/Users/ultra/xp/task-cli/tasklib/transitions.py`
  - `/Users/ultra/xp/task-cli/tasklib/session.py`
  - `/Users/ultra/xp/task-cli/tasklib/classify.py`
  - `/Users/ultra/xp/task-cli/tasklib/daemon.py`
- rig-cli:
  - `/Users/ultra/xp/rig-cli/docs/config-schema.md`
  - `/Users/ultra/xp/rig-cli/docs/specs/rig-cross-harness-provisioning.md`
  - `/Users/ultra/xp/rig-cli/riglib/stats/model.py`
  - `/Users/ultra/xp/rig-cli/riglib/stats/aggregate.py`
  - `/Users/ultra/xp/rig-cli/riglib/stats/command.py`
  - `/Users/ultra/xp/rig-cli/riglib/stats/sources/codex.py`
- agent-tools:
  - `README.md`
  - `AGENTS.md`
  - `agent-hooks/orchestrator-stays-thin/orchestrator_stays_thin.py`
  - `agent-hooks/background-subagent-gate/background_subagent_gate.py`
  - `lib/codex_hook_bridge/README.md`
  - `lib/opencode_hook_bridge/README.md`
  - `lib/agenttools_stall_watchdog/README.md`
  - `lib/contracts/models.yaml`
- tg-cli:
  - `/Users/ultra/xp/tg-cli/README.md`
  - `/Users/ultra/xp/tg-cli/CHANGELOG.md`
  - task records #171 and #172
- research-cli:
  - `/Users/ultra/xp/research-cli/README.md`
  - `/Users/ultra/xp/research-cli/research_cli/providers.py`
  - `/Users/ultra/xp/research-cli/research_cli/engine.py`
  - `/Users/ultra/xp/research-cli/research_cli/transport.py`

### Runtime Evidence

- `rig stats show --format json --since 2026-07-01 --repo /Users/ultra/xp/agent-tools`
  found 8,503 invocations, supported harnesses `claude-code`, `codex`, `gemini`, and
  `opencode`, and an adoption ratio of 0.1508. `task (cli)` had 246 calls, `review (cli)`
  90, `tg (cli)` 84, and `rig (cli)` 9. The large "other" bucket was dominated by harness
  control operations such as `write_stdin`, `wait_agent`, `spawn_agent`, and `close_agent`.
- `rig status -C /Users/ultra/xp/.worktrees/agent-tools-255-pm-agent` failed before drift
  reporting because the live global config contains `harness.kinds`, while installed
  rig 0.12.0 rejects that key. This is a concrete PM-agent health-check requirement:
  configuration/schema skew can hide real drift.
- `~/.config/agent-tools/ship-audit.jsonl` contains quorum gate audit rows with authorized
  and refused decisions. PM-agent should use this as one deployment-evidence input rather
  than relying on ticket state alone; it must not become the sole authority until the
  audit path has tamper-evidence or permission hardening.
- `~/.cache/agent-tools/overrides.log` contains explicit protect-main overrides, including
  multiple Dive Calc deploy-related overrides. PM-agent reports should surface these as
  operational exceptions.
- `~/.local/state/task-cli/sessions/2.jsonl` shows repeated touches of the same active
  tickets across repos, which reinforces the need for de-duplicated session/task views.

## Current Responsibility Boundaries

- task-cli owns durable ticket data: structured fields, normalized state, session labels,
  classification, due-date reminders, acceptance proof gates, and backend mapping.
- tg-cli owns Telegram intake, routing, questions, usage-limit notifications, `/new`
  spawning, `/limit`, and outbound status/reporting.
- rig-cli owns catalog reconciliation, harness skill/hook provisioning, autonomous-mode
  policy, model freshness, branch/server-side setup, and cross-harness tool statistics.
- agent-tools owns portable content and enforcement: skills, agent-hooks, git-hooks, CI
  gates, ship gate, hook bridges, and reusable libraries such as the stall watchdog.
- review-cli and research-cli are external advisory/research engines, not state owners.

The PM-agent should therefore be a coordinator and state machine over these systems, not a
new ticket backend, provisioning engine, Telegram daemon, or implementation worker.

## Proposed Architecture

### Core Shape

Build a small PM-agent service/CLI as a new ecosystem tool, with a durable store and
adapters around existing tools:

- `pm ingest`: consume Telegram messages, task records, review outputs, ship audit rows,
  rig stats/status, harness limit events, and watchdog events.
- `pm plan`: update work-item state, assign owners/executors, detect dependencies, and
  decide next actions.
- `pm dispatch`: create tickets or subagent briefs, choose harness/model, and start
  background work through existing tg/rig/harness surfaces.
- `pm reconcile`: poll or subscribe to task/tg/rig/review/CI/ship evidence and advance
  states.
- `pm report`: produce queue, stuck-work, retrospective, and monthly product/process
  reports.

The durable store can start as append-only JSONL plus a compact SQLite projection. JSONL
gives auditability and easy recovery; SQLite gives queryable dashboards and monthly reports.

### Flexible FSM

Do not hard-code task-cli's five states as the PM-agent model. Keep those as one adapter
projection. The PM model should support configurable state definitions with categories and
required evidence.

Suggested default state categories:

- `intake`: seen, classified, deduped, ticketed
- `triage`: needs-clarification, ready, blocked-by-decision, blocked-by-dependency
- `planned`: scoped, assigned, queued, scheduled, waiting-for-slot
- `active`: dispatched, implementing, reviewing, fixing, verifying
- `delivery`: pr-open, ci-pending, ship-ready, shipping, deployed, deploy-verifying
- `post`: done, parked, cancelled, superseded, needs-retro, retro-done
- `exception`: stuck, limit-wait, provider-overload, harness-missing, config-drift,
  human-required

Each state should define:

- `terminal`: boolean
- `allowed_next`: explicit adjacency list or category transition rule
- `owner_required`: human, pm-agent, subagent, external CI, none
- `evidence_required`: task update, PR URL, CI success, ship audit row, deployment probe,
  screenshot, review marker, Telegram confirmation, or explicit skip reason
- `sla`: optional timers for reminders/escalation
- `projection`: how it maps into task-cli state/labels/comments

This allows `todo/in-progress/done` as projections without losing richer lifecycle data.

### Dependency Reminders

Represent dependencies as typed edges:

- `blocks`: A must finish before B can proceed.
- `waits_for`: external condition such as CI, deployment, user answer, quota reset.
- `duplicates`: two tickets/messages describe the same work.
- `supersedes`: new ask replaces old plan.
- `relates_to`: context only, not gating.

Reminders should trigger on:

- blocked item whose blocker changed state
- waiting item whose timer expired
- active item with no observed progress
- dependency cycle
- child task done while parent not advanced
- parent requested "done" but child lacks deployment/verification evidence

### Telegram Intake To Deploy Tracking

Every inbound Telegram/request should become an `intake_event` immediately with:

- Telegram message id/thread/topic, sender, timestamp, raw text/caption, attachments
- detected repo/project/session
- classifier verdict and confidence/source
- dedupe candidates
- resulting task id(s), follow-up ideas, and owner

Lifecycle should not stop at `task done`. The delivery chain should require:

1. request captured
2. ticket created or linked
3. subagent/worktree assigned
4. PR opened or explicit no-code path recorded
5. review evidence captured
6. CI green
7. ship gate audit row or documented bypass
8. deployment observed
9. smoke/visual/probe evidence captured
10. requester/status thread updated
11. retrospective queued if the work had exceptions

### Token And Harness Limit Management

PM-agent should maintain an executor ledger:

- harness kind, model, pane/session id, repo/worktree
- current limit telemetry and reset time
- context-window pressure
- active tasks and estimated remaining cost
- reliability status: healthy, degraded, overloaded, limit-wait, no-telemetry

Routing policy:

- Low-risk docs/triage/read-only research: cheaper junior-capable models.
- Normal implementation: capable model with repo/harness support and available budget.
- High-risk architecture/security/release decisions: Fable, Sol if configured, Opus-class,
  or equivalent senior models with review quorum.
- Near-limit harness: do not start long work; hand off to another harness or schedule a
  reset wakeup.
- Hard-limit: record a limit-wait state, notify/report, and re-dispatch elsewhere when
  possible.

The task request mentions resetting Codex limits when policy allows. That should not be an
implicit action. Model it as `limit_reset_available`, `reset_requires_explicit_redeem`, and
`reset_redeemed` events, with a policy knob that decides when PM-agent may spend a reset.

### Stuck-Task Control

Stuck detection should combine:

- stall watchdog WARN/ABORT events
- no task/status/PR/log progress within SLA
- repeated failed review iterations
- repeated provider overload auto-continues
- CI pending beyond expected duration
- review thread unresolved beyond SLA
- worktree dirty without commits for too long
- subagent foreground/inline orchestration violations

Default action ladder:

1. nudge the responsible agent/pane
2. inspect minimal evidence automatically
3. re-dispatch or split to another executor
4. park with reason if external dependency is real
5. alert Alex only for ABORT-tier, human decision, or unrecoverable conflict

### Retrospectives And Monthly Reports

Every exception-class item should accumulate retro facts:

- what triggered it
- detection latency
- recovery action
- repeated pattern or one-off
- task/repo/harness/model affected
- proposed prevention

Monthly reports should group by:

- Product delivery: shipped features, bug fixes, deployments, rollbacks, parked work,
  user-visible outcomes.
- Development process: gate bypasses, stuck-task count, average recovery time, harness
  utilization, model routing cost, review iteration counts, rig drift, missed telemetry,
  recurring blockers, and automation improvements proposed.

### Autonomous Loops And Triggers

Suggested loops:

- Intake loop: watches Telegram/tg history/task classify outputs.
- Queue loop: reconciles task records, session sidecars, worktrees, PRs, CI, ship audit.
- Executor loop: consumes tg-cli limit telemetry and harness logs.
- Stuck loop: consumes watchdog and no-progress timers.
- Reporting loop: daily queue/status, weekly retro digest, monthly product/process report.
- Learning loop: during low-work periods, read approved management/process material and
  propose one project-specific experiment as a decision request, not as an automatic change.

### Orchestrator-Only Behavior

PM-agent must not implement. It can:

- create/update/link tickets
- spawn/assign subagents
- ask review/research tools
- inspect status/evidence
- send Telegram reports/questions
- mark workflow states when evidence is present

It must not:

- edit product code
- run implementation test/fix loops itself
- commit or ship directly
- resolve human review threads without the established authority rules
- bypass hook/ship/review gates

## TypeScript Interface Sketch

```ts
type WorkStateCategory =
  | "intake" | "triage" | "planned" | "active" | "delivery" | "post" | "exception";

interface WorkStateDefinition {
  id: string;
  category: WorkStateCategory;
  terminal?: boolean;
  allowedNext: string[];
  evidenceRequired?: EvidenceKind[];
  slaSeconds?: number;
  taskProjection?: "todo" | "in-progress" | "in-review" | "done" | "cancelled";
}

type EvidenceKind =
  | "telegram-message" | "task-record" | "pr" | "review" | "ci" | "ship-audit"
  | "deployment" | "smoke" | "screenshot" | "limit-telemetry" | "watchdog"
  | "human-decision" | "skip-reason";

type PrivacyClass = "local-only" | "remote-allowed";

interface OwnerRef {
  kind: "human" | "agent" | "service";
  id: string;
  label?: string;
}

interface EvidenceRef {
  kind: EvidenceKind;
  uri: string;
  observedAt: string;
  digest?: string;
}

interface TimerRef {
  kind: "sla" | "retry" | "quota-reset" | "review-dwell" | "reminder";
  dueAt: string;
}

interface WorkItem {
  id: string;
  title: string;
  state: string;
  projectId: string;
  privacy: PrivacyClass;
  taskRefs: string[];
  intakeRefs: string[];
  owner?: OwnerRef;
  executor?: ExecutorRef;
  dependencies: DependencyEdge[];
  evidence: EvidenceRef[];
  timers: TimerRef[];
  updatedAt: string;
}

interface DependencyEdge {
  type: "blocks" | "waits_for" | "duplicates" | "supersedes" | "relates_to";
  targetId: string;
  reason: string;
  condition?: WaitCondition;
}

interface WaitCondition {
  // Only meaningful for `waits_for` edges; omit for duplicate/supersession links.
  provider: "ci" | "telegram" | "watchdog" | "time" | "quota" | "deployment";
  subject: string;
  expected: string;
}

interface ExecutorRef {
  harness: "claude-code" | "codex" | "opencode" | "gemini" | "pi" | string;
  model?: string;
  paneId?: string;
  worktree?: string;
  status: "healthy" | "degraded" | "overloaded" | "limit-wait" | "no-telemetry";
}

interface Adapter<TEvent> {
  name: string;
  poll(since?: string): Promise<TEvent[]>;
  subscribe?(callback: (event: TEvent) => void): Promise<() => void>;
  apply?(command: AdapterCommand): Promise<AdapterResult>;
}

interface AdapterCommand {
  idempotencyKey: string;
  target: "task" | "telegram" | "rig" | "review" | "github" | "harness";
  action: string;
  payload: Record<string, unknown>;
}

interface AdapterResult {
  ok: boolean;
  evidence?: EvidenceRef[];
  error?: string;
}
```

## Integration Responsibilities

- task-cli: keep current ticket contract; add optional labels/comments/fields for PM
  projections only if needed. Do not force rich PM states into `State`.
- tg-cli: expose durable intake events and limit/harness events; PM-agent consumes them.
- rig-cli: expose machine/repo health, harness capabilities, model manifest, stats, drift,
  and provisioning state. PM-agent consumes them and can request `rig apply` through a
  delegated worker, not inline.
- agent-tools: provide skills/hooks/gates/watchdog events. PM-agent obeys orchestrator-only
  policy and treats hook blocks as process events.
- review-cli/research-cli: advisory engines. PM-agent records model composition and outputs
  as evidence, never as state authority.

## Validation Plan

- Unit tests for FSM adjacency, evidence gates, dependency reminders, routing decisions,
  timer escalation, dedupe, and report grouping.
- Fixture projects with fake task/tg/rig/CI/ship logs.
- Docker or equivalent isolated fixtures for:
  - GitHub-like task lifecycle
  - tg intake history
  - harness limit events
  - rig status/drift output
  - ship audit rows
  - stuck log/watchdog transitions
- Golden monthly reports from fixture history.
- End-to-end dry run: Telegram request -> ticket -> fake subagent -> fake PR -> fake CI
  -> fake ship -> fake deploy probe -> final Telegram report.

## External Tool Notes

- `research-cli` was not installed on `PATH`; it was runnable from
  `/Users/ultra/xp/research-cli/bin/research`.
- `research board` resolved three seats: Analyst `claude-opus-4-8`, Skeptic
  `claude-fable-5`, and Scout `gemini-2.5-flash`.
- `research ask --offline --json ...` worked, but only with stub answers. The README says
  live calls require `RESEARCH_BACKEND_CMD`; multi-round/citations/model synthesis are
  still roadmap items. It is useful for structure but not sufficient for #255's substantive
  design research yet.
- `research-cli` checkout is `main...origin/main [ahead 1, behind 1]`, and its `AGENTS.md`
  is still a placeholder. That should be fixed before relying on it for autonomous work.
- `Sol` was not found in the local shared model manifests or review/research configs.
  `Fable` is present as `claude-fable-5`. The #255 requirement for "Fable and Sol with
  effort selected per phase" therefore needs a model-manifest/config follow-up.
- `review brainstorm` completed for task 255 with Opus/Codex/Gemini/GLM seats and an
  Opus moderator. The raw discussion log is
  `/Users/ultra/Library/Logs/review-cli/20260711T144730_967810Z-brainstorm.md`.
  It is not kept under repo docs because the generated raw transcript includes non-English
  moderator text; the durable repo artifact is this English distillation.

## Review Brainstorm Distillation

The multi-model brainstorm pushed the design toward a deliberately smaller first release:
a short-lived, event-triggered PM `tick` that observes, reconciles, reports, and explains
state before it dispatches implementation work. A resident daemon can wrap that tick later,
but the first invariant should be that a crash/restart can replay events and reach the same
queue projection.

Release 1 should be an "organ of sight": reconcile task records, Telegram intake, PR/CI
state, rig health, ship audit, and watchdog/limit events; provide queue reports, `pm why`,
privacy scans, and doctor checks; do not autonomously dispatch. Release 2 can add dispatch
behind a hard integration test: a `privacy_class: local-only` work item must never become a
remote model prompt or code-editing executor under throttle, fallback, kill-switch, and
crash-restart races.

The brainstorm strongly favored keeping task-cli's canonical states stable and projecting
PM richness through labels/comments/PM metadata such as `pm.stage`, `pm.blocked`,
`pm.health`, and evidence references. Add new task-cli top-level states only through a
separate task-cli migration, because `todo/in-progress/in-review/done/cancelled` are already
shared by task, Gantt, ship, and session tooling.

Terminal delivery states must be based on evidence the PM-agent cannot forge. `done` and
`deployed` should require external facts such as GitHub merge/CI state, deployment probes,
and ship-audit rows. Ship audit is useful as one signal today, but autonomous dispatch needs
tamper-evidence or path/permission hardening before using it as sole authority.

Enforcement should prefer capability deprivation over advisory hooks. The PM harness should
lack Edit/Write/commit/merge/ship capability; implementation happens only through delegated
workers. Existing hooks are still useful signals, but Codex has no trusted `pre-agent`
mapping yet and `orchestrator-stays-thin` is fail-open/warn-first, so those hooks are not
sufficient as the only boundary.

Model fallback is a major control-plane risk. It must become per-task and privacy-aware,
with explicit budget/zero-spend behavior, cooldown/oscillation control, signed or private
state, and kill-switch coordination across sentinel, bridge registry, and running harnesses.
Fallback must not quietly route local-only work or high-risk tasks to a cheaper or less
trusted model.

The current ecosystem has invocation statistics, not reliable spend accounting. `models.yaml`
does not carry pricing, and rig stats count tool calls rather than token cost. V1 should
therefore implement quota and limit guarding, not pretend to be a complete cost accountant.

Intake needs a dead-letter queue. Every inbound Telegram/request event should be recorded as
`intake:unprocessed` before classification; retries should be bounded; repeated failures
should move to `intake:deadletter` and generate one durable report rather than being lost.

Crash safety should use intent-to-apply records or small transactions for every mutation
that touches more than one system. Half-applied task/TG/PR updates are a more likely failure
mode than bad FSM adjacency.

Autonomous improvement should stay reviewable. The PM-agent can propose policy/process
changes, retrospectives, and learning experiments, but it should not auto-apply learned
process rules without a decision gate or PR.

Suggested adversarial tests before autonomous dispatch:

- fallback oscillation and fallback-vs-kill-switch race
- budget/limit exhaustion during dispatch
- privacy-violating fallback attempt
- half-applied multi-system mutation
- dead-letter retry exhaustion
- ship-audit tamper or missing external merge evidence
- world-writable state directory or unsigned override/replay
- duplicate dispatch after crash/restart
- per-repo/worktree/ship lease collision
- spoofed actor in mutation logs

## Open Risks

- PM-agent can become a shadow implementation agent unless orchestrator-only rules are
  explicit and enforced.
- Rich PM statuses may diverge from task-cli if projection rules are not deterministic.
- Harness telemetry is uneven; Codex and opencode still have trusted-identity and hook
  surface gaps.
- `rig status` currently fails on global config schema skew, so health checks must handle
  "tool/config mismatch" as a first-class state.
- Deployment evidence differs by project; the first version needs pluggable deploy probes
  and explicit "no deploy" evidence.
- Monthly reports can become vanity metrics unless every number links to underlying events.
- Autonomous learning from books/external material needs a source whitelist and decision
  gate so PM-agent proposes techniques rather than silently changing process.

## Recommended Next Tasks

1. Create a PM-agent spec in a new repo or `docs/specs/` with the event schema, FSM config
   format, adapter contracts, and storage choice.
2. Add a small fixture suite that replays Telegram intake, task records, rig stats,
   ship audit, limit events, and watchdog events into a projected queue.
3. Fix or update rig global config/schema skew around `harness.kinds` so PM-agent can rely
   on `rig status`.
4. Decide where `Sol` lives in the shared model manifest and how "effort selected per
   phase" should be represented for review/research tools.
5. Bring research-cli to autonomous readiness: install on PATH, replace placeholder
   AGENTS.md, resolve branch drift, add live transport/model composition recording, and
   support the Fable/Sol panel requirement.
