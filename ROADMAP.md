# Agent CLI Ecosystem — Roadmap

Status snapshot: **2026-06-15**. This is the handoff/roadmap for the agent-native CLI ecosystem
(`tg-cli`, `review-cli`, `rig-cli`, `draw-cli`, `3d-cli`, `task-cli`) + the `agent-tools` umbrella.

> **North-star architecture (DECIDED — do not re-litigate):** the whole CLI ecosystem is
> **Python-only**. `tg-cli` (currently Bun/TS) migrates to Python **LAST**. The shared
> `agent-tools/lib` is a **plain Python library** (NO `lib/ts`, NO cross-language contracts).
> New tools are built in Python on that lib so a new tool spins up fast on one architecture.
> `rig` is the front door (`rig init` onboarding, `rig apply` reconcile — two distinct commands,
> interactivity orthogonal). `agent-tools` = WHAT (the catalog), `rig` = HOW (installs it).

---

## ✅ Done (this session, 2026-06-15)

### review-cli
- ✅ **Modes → first-class SUBCOMMANDS** (`review review` default, `brainstorm`, `just-ask`,
  `quorum`; mode-registry plugin-dirs; `--brainstorm/--quorum/--just-ask` flags removed; bare
  `review -C <repo>` still defaults to diff-review; `--visual` stays a composable flag) — PR #23 **merged**.
- ✅ README reframed: visual review surfaced, spec-web/dashboard de-headlined → "spec-review
  tooling", removed agentic-marketing phrasing, ralphex comparison kept with two SVG diagrams.
- ✅ Dashboard `--host` Tailscale exposure + **live SSE** activity stream — PR #21 merged.
- ✅ Board: agentic codex seat (GPT-5.5 = codex); Kimi-K2.7-Code on board; pool=4 default.
- ✅ Ecosystem doc migration: `~/.claude/CLAUDE.md` + `review install-skill` regenerated to subcommand syntax.

### tg-cli
- ✅ `tg replies [user|agent|all] [list|find]` — recall session messages (history.jsonl) — PR #24 merged.
- ✅ Inbound media download retry-with-backoff — PR #22 merged.
- ✅ README orthogonality: branding-follows-model (not opencode-specific), detection precedence,
  3-axis comparison — PR #23 merged.

### rig-cli
- ✅ `rig init` onboarding front door + `rig apply` reconcile + auto-mode harness provisioning
  (writes `permissions.defaultMode`) — PR #3 merged. Installed + verified (doctor green).
- ✅ Global layer applied machine-wide (47 skills → ~/.agents/skills, 7 agent-hooks, git-dispatcher, ship, mcp).

### agent-tools
- ✅ README reframed around rig front door + 4 pillars (autonomous/observable/controllable/safe).
- ✅ New agent-hook `block-raw-pr-merge` (forces `gh ship`, escape-hatch) — PR #8 merged.
- ✅ Shared-lib architecture **design doc** at `docs/specs/2026-06-15-shared-lib-architecture.md`
  (NOTE: its `lib/ts`/contracts-twinning part is SUPERSEDED — Python-only).

### ecosystem hygiene
- ✅ ralphex/quorex removed everywhere + `alex-mextner/quorex` **archived** with a "superseded by
  review-cli" banner; 3d-cli docs purged; hyper-saas AGENTS.md ralphex workflow removed (PR #461 merged).
- ✅ hyperide.ai homepage URL cleared on all 6 tool repos.

---

## 🚧 In-flight at handoff (background agents — verify state, resume if incomplete)
- **task-cli foundation** (agent a9cef4ca) — building the Python tool in the new `alex-mextner/task-cli`
  repo per `task-cli/docs/2026-06-15-task-cli-spec.md`. Check the repo for the pushed branch/PR.
- **model-currency** (agent a48e44ab) — building `lib/contracts/models.yaml` (→ relocate to a
  Python `lib/` path, no contracts dir needed) + `lib/checker/model_freshness.py` + rig daily-noon
  cron provisioning. Check agent-tools + rig-cli for its PRs.
- **CI-gate resilience** — DONE: agent-tools **PR #9** (gate templates degrade gracefully); wave-1
  rollout PRs patched green except tg-cli #25 CodeQL.

## 📋 Open PRs ready to land
- agent-tools **#9** — CI gate resilience (green).
- rollout: draw-cli **#3** (green), 3d-cli **#6** (green), tg-cli **#25** (CodeQL JS/TS flags **8 real
  pre-existing TS findings** — fix-or-justify by owner before merge).

---

## 🗺️ Remaining work (prioritized)

### 1. task-cli (after foundation lands)
- **Phase 1** (foundation, in-flight): Python CLI — backends (GitHub Issues default, Linear per-repo)
  call the provider **API directly** with creds harvested from `gh`/`linear` configs; classification
  `change|justAsk` via `review just-ask` per-provider fallback chain (haiku head); enforcement gates
  (acceptance criteria / motivation / user-impact / cost-of-inaction / screenshots / formatting, each
  with `--skip-<gate> "<reason>"` escape hatch); `list` defaults to this-session tickets.
- **Phase 2** (#3695): task **dependency system** + **Gantt** rendering; a **daemon service** (like
  tg-ctl: first call brings it up, survives restarts/kills, subscribes to gh-issues/Linear **webhooks**,
  **adapter-based** for future trackers); on completion → inject "done, X unblocked" into the agent's
  tmux pane (via shared-lib tmux-inject); **due-date** reminders to the agent; task-cli self-hook.
- **Integrations**: tg-cli classify inbound hook (#3645); agent-tools `strict-ticket-discipline`
  skill + `require-ticket-before-commit` guard (+ optional `ci/ticket-required`); rig provisioning
  (installs linear-cli when backend=linear, for auth only). Needs a NEW `on-inbound` hook point in
  `agents-hooks/v1`.

### 2. tg-cli `/tasks` (after task-cli)
- `/tasks` → table + Gantt + agent summary for the session's tasks; show external-to-session deps;
  diagram sent **HD inline** (not as a file, uncompressed-quality).

### 3. Shared lib extraction (Python-only) — `agent-tools/lib/<module>`
Per the design doc; phased, lowest-churn first. Each tool migrates to import from the lib.
1. **`advertise`** (install-skill — 4 copies today) — first extraction, stands up the skeleton.
2. **`hooks`** (`agents-hooks/v1` dispatcher; tg vendored it — de-dupe).
3. **`providers`** (the biggest asset: board, failover, transports, `oc:` routing, key cascade,
   **capability tags incl. vision-filter for `--visual`**, **model-currency manifest**). Consumed by
   review, task-cli classifier, research-cli.
4. **`config`** (cascade + env precedence + `~/.config` paths).
5. **`registry` + trust-kernel** (the hook-trust + visual-module-registry trust algorithm, one place).
6. **`retry`** (net-new), **`tmux-inject`** (extract from tg-ctl), **daemon-supervisor**
   (survives kills — shared by task-cli + tg-ctl), **Gantt-render**, `agenttools_log` (exists).

### 4. model-currency (in-flight → finish)
- `models.yaml` manifest: per-provider current model + **capability tags** (vision/code/reasoning) +
  role aliases (role `vision` resolves only to vision-capable). Daily-noon **cron checker** that polls
  provider `/models` and opens a bump PR (semi-auto). rig provisions the schedule (launchd/cron),
  check-and-install-if-missing at init/apply.

### 5. rig
- **Enable repo security settings at init/apply** (#3696/#85): `gh api` enable Dependency Graph +
  vuln-alerts (+ secret-scanning) so the CI gates run instead of skipping. (Done manually on wave-1.)
- **Auto-mode is LOCAL**: `.claude/settings.json` must be **gitignored** (per-machine via apply);
  `rig.yaml` `harness.auto_mode` is the committed declaration. Fix rig to gitignore it on apply.
  (Manually corrected on wave-1 PRs; rig-cli `.claude/settings.json` committed in d87257a needs un-commit.)
- **Rollout (#3686)**: wave-1 tool repos = PRs open (draw/3d green, tg pending TS fix). **Wave-2 =
  bots** (ExpenseSyncBot, garage-band, summary-bot, esphome-ir, claude-p, diploma, sme-archiving-gc,
  talks, upwork) + review-cli/rig-cli/agent-tools themselves. Each: `rig init --yes` + conservative
  AGENTS/CLAUDE slim (drop now-self-advertised generic rules, keep project specifics) + harvest report.

### 6. research-cli (after lib `providers`)
- Separate Python CLI on the shared lib's panel engine (NOT a review mode). Reuses providers verbatim.

### 7. review-cli follow-ups
- #75: make ALL board models agentic via opencode (re-investigate commandcode + GLM custom provider).
- Fix flat `DEFAULT_MODELS` stale `kimi-k2p6-turbo` via Fireworks (dead `glide` account) → manifest.
- Propagate canonical ecosystem one-liner to other repos' ecosystem tables (after their rollout PRs):
  *"multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual
  review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic."*

### 8. agent-tools harvest (one centralized pass, from rollout reports)
- Candidate skills: **worktree-via-project-CLI** (dep provisioning, distinct from worktree-base-trap),
  **subagent-delegation contract**, **"diagnostic image ≠ proof"** acceptance bar, **queued-report
  durability** (channel-unavailable → don't fake delivery). (Sources: 3d-cli AGENTS.)
- `strict-ticket-discipline` skill + `require-ticket-before-commit` guard (with task-cli).

### 9. Misc / cleanup
- **Slim `~/.claude/CLAUDE.md` to PERSONAL-ONLY** (#3704): it should hold only user-specific content
  (identity / "address me as Alex" / preferences / dictionary / Telegram report style). ALL universal
  tool + process docs (how to use `review`/`tg`/`draw`, the no-short-timeout rule, etc.) must
  self-advertise via SKILLS, not be hand-written there — many already have skills + the `<!-- skill:X -->`
  blurbs are the self-advertising mechanism. The hand-written "## Review and sanity checks" section
  duplicates the `review` skill → remove it; audit the rest the same way. (My in-place "migrate CLAUDE.md
  review docs to subcommands" was wrong — those docs shouldn't live in CLAUDE.md at all.) Careful pass,
  it's the global config.
- **Decisions-as-buttons + the hanging-question DANGER** (#3706): verify the agent
  question-with-options flow (tg inline tappable buttons) actually works end-to-end. **Critical
  bug risk:** `tg-ctl` injects inbound messages into the agent's tmux pane via `send-keys`; if an
  interactive prompt/question is open in that pane, the injected text is typed INTO the prompt and
  LOST/corrupts it. Fix: answers must come through a SEPARATE channel (tg inline buttons → routed
  reply), and while a question is pending tg-ctl must DEFER/queue inbound text injection (detect the
  pending-question state) rather than blast it into the pane. Test + fix.
- **tg-cli `tg#<id>` message-ref convention** (tg#3715): reference inbound TG messages as `tg#3715`
  (NOT bare `#3715` — collides with PR/issue refs). The inbound inject wrap should render the id as
  `tg#<id>`; the autolink layer must recognize `tg#\d+` as a message reference and link it, **before**
  the PR/issue `#\d+` detection runs. ("— reply via tg" suffix already removed, tg-cli main 83ac5db.)
- **tg-cli file-excerpt attachment** (tg#3715): when a message gives a file **path + line / line-range**
  (in any of the common formats — `path:42`, `path:10-20`, `path#L10-L20`, etc.), attach the actual
  excerpt (the referenced lines) as a quote below. Currently broken — fix.
- **tg-cli `/new` command** (tg#3717): `/new [<model>] [<dir>] name [<task description>]` — spawn a new
  agent session. **Omitted options are chosen interactively via inline buttons.** Directory options =
  the dirs already in use + their `..` parents, all normalized + uniq + ranked LRU/MRU. `name` validated
  with a **uniqueness warning**. (Pairs with the decisions-as-buttons work.)
- tg-cli #25: fix-or-justify the 8 CodeQL TS findings (length-counting helpers + install TOCTOU).
- tg-cli AGENTS stale "~578 tests" → 997.
- hyper-saas AGENTS.md "further-trim candidates" (Systematic Debugging / Dead-code / On-Task-Completion
  / Naming / Codex-Specific) — CTO to confirm before trimming (generic blocks now covered by skills).
- Billing: Fireworks/Fire Pass (`glide` account) suspended — only that provider is down; rest work.

---

## Key constraints / lessons (read before continuing)
- **Python-only ecosystem; tg migrates last; lib is plain Python.** (Decided twice — don't re-ask.)
- **Every provider/harness write-up should be GENERAL, not special-cased** (no "opencode does X" when
  every harness does X). Models marked by **capabilities**, not just version.
- **auto-mode `bypassPermissions` = local (gitignored), not committed.**
- **Use `gh ship`, not raw `gh pr merge`** (the `block-raw-pr-merge` guard enforces it).
- Tool repos work directly on `main` (push often); hyper-saas via PR + `gh ship`.
- This roadmap lives in `agent-tools` because ecosystem work should run from a tool repo / agent-tools,
  not from an unrelated product repo.
