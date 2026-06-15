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
- ✅ **Auto-mode provisioned (self-dogfood)** — committed `rig.yaml` (`harness.auto_mode: true`) +
  gitignored local `.claude/settings.json` (`bypassPermissions`). agent-tools was a pending wave-2
  target that had never been run through rig (no rig.yaml at all); now done. Installed `rig` still
  doesn't gitignore the settings file (rig-cli#6 unfixed), so the `.gitignore` lines were added by hand.

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
- agent-tools **#9** — CI gate resilience — **MERGED**.
- rollout: draw-cli **#3** — **MERGED**; 3d-cli **#6** — **MERGED**; rig-cli **#4** (model-cron
  schedule) — **MERGED** (gitleaks GITHUB_TOKEN fix + 2 codex review findings resolved).
- **tg-cli #25** (rig rollout) — **MERGED** with a documented justification: it is infra-only
  (CI workflows + `ci/` companions + AGENTS.md + rig.yaml), touches no `features/**` source, and the
  CodeQL JS/TS self-gate's only red was **8 pre-existing findings** in untouched `features/**` code
  (the gate firing on legacy code, not a regression). Those are tracked for a real fix-or-justify at
  alex-mextner/tg-cli#31 — NOT mass-suppressed; the self-gate goes green on main once #31 lands.

---

## 🗺️ Remaining work (prioritized)

### 1. task-cli (after foundation lands)
- **Phase 1** (foundation, in-flight): Python CLI — backends (GitHub Issues default, Linear per-repo)
  call the provider **API directly** with creds harvested from `gh`/`linear` configs; classification
  `change|justAsk` via `review just-ask` per-provider fallback chain (haiku head); enforcement gates
  (acceptance criteria / motivation / user-impact / cost-of-inaction / screenshots / formatting, each
  with `--skip-<gate> "<reason>"` escape hatch); `list` defaults to this-session tickets.
- **Phase 2** (#3695): task **dependency system** + **Gantt** rendering (alex-mextner/task-cli#1); a
  **daemon service** (like tg-ctl: first call brings it up, survives restarts/kills, subscribes to
  gh-issues/Linear **webhooks**, **adapter-based** for future trackers) (alex-mextner/task-cli#2); on
  completion → inject "done, X unblocked" into the agent's tmux pane (via shared-lib tmux-inject);
  **due-date** reminders to the agent (alex-mextner/task-cli#3); task-cli self-hook.
- **Integrations** (alex-mextner/task-cli#4): tg-cli classify inbound hook (#3645); agent-tools
  `strict-ticket-discipline` skill + `require-ticket-before-commit` guard (+ optional
  `ci/ticket-required`); rig provisioning (installs linear-cli when backend=linear, for auth only).
  Needs a NEW `on-inbound` hook point in `agents-hooks/v1`.

### 2. tg-cli `/tasks` (after task-cli)
- `/tasks` → table + Gantt + agent summary for the session's tasks; show external-to-session deps;
  diagram sent **HD inline** (not as a file, uncompressed-quality). (alex-mextner/tg-cli#26)

### 3. Shared lib extraction (Python-only) — `agent-tools/lib/<module>`
Tracking issue: alex-mextner/agent-tools#12. Per the design doc; phased, lowest-churn first. Each
tool migrates to import from the lib.
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
  check-and-install-if-missing at init/apply. (alex-mextner/rig-cli#8 — rig cron provisioning landed
  in PR #4, merged.)

### 5. rig
- **Enable repo security settings at init/apply** (#3696/#85): `gh api` enable Dependency Graph +
  vuln-alerts (+ secret-scanning) so the CI gates run instead of skipping. (Done manually on wave-1.)
  (alex-mextner/rig-cli#5)
- **Auto-mode is LOCAL**: `.claude/settings.json` must be **gitignored** (per-machine via apply);
  `rig.yaml` `harness.auto_mode` is the committed declaration. Fix rig to gitignore it on apply.
  (Manually corrected on wave-1 PRs; rig-cli `.claude/settings.json` committed in d87257a needs un-commit.)
  (alex-mextner/rig-cli#6)
- **Rollout (#3686)**: wave-1 tool repos = PRs open (draw/3d green, tg pending TS fix). **Wave-2 =
  bots** (ExpenseSyncBot, garage-band, summary-bot, esphome-ir, claude-p, diploma, sme-archiving-gc,
  talks, upwork) + review-cli/rig-cli/agent-tools themselves. Each: `rig init --yes` + conservative
  AGENTS/CLAUDE slim (drop now-self-advertised generic rules, keep project specifics) + harvest report.
  (alex-mextner/rig-cli#7)
  - **Auto-mode provisioning status (verified on `origin/main`, 2026-06-15):** rig-cli ✅, draw-cli ✅,
    3d-cli ✅, agent-tools ✅. Still missing: **review-cli** (in-sync local, no `rig.yaml`), **task-cli**
    (foundation in-flight). **tg-cli** is Bun/TS → migrates to Python last; provision auto-mode after
    that migration. (Earlier "draw/3d unprovisioned" reads were stale local checkouts — both carry
    `rig.yaml` on origin.)

### 6. research-cli (after lib `providers`)
- Separate Python CLI on the shared lib's panel engine (NOT a review mode). Reuses providers verbatim.
  (alex-mextner/agent-tools#13)

### 7. review-cli follow-ups
- #75: make ALL board models agentic via opencode (re-investigate commandcode + GLM custom provider).
  (alex-mextner/review-cli#24)
- Fix flat `DEFAULT_MODELS` stale `kimi-k2p6-turbo` via Fireworks (dead `glide` account) → manifest.
  (alex-mextner/review-cli#25)
- Propagate canonical ecosystem one-liner to other repos' ecosystem tables (after their rollout PRs):
  *"multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual
  review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic."*
  (alex-mextner/review-cli#26)

### 8. agent-tools harvest (one centralized pass, from rollout reports)
Tracking issue: alex-mextner/agent-tools#14.
- Candidate skills: **worktree-via-project-CLI** (dep provisioning, distinct from worktree-base-trap),
  **subagent-delegation contract**, **"diagnostic image ≠ proof"** acceptance bar, **queued-report
  durability** (channel-unavailable → don't fake delivery). (Sources: 3d-cli AGENTS.)
- `strict-ticket-discipline` skill + `require-ticket-before-commit` guard (with task-cli).

### 9. Misc / cleanup
- **PREREQUISITE for ALL AGENTS.md/CLAUDE.md slimming — skills must actually LOAD** (tg#3736/3740,
  rig-cli#9): the harness discovers Skill-tool skills from `~/.claude/skills/` (symlinks → ~/.agents/skills),
  NOT from ~/.agents/skills directly. rig installed to ~/.agents/skills but never symlinked into
  ~/.claude/skills → rig-installed universal skills were NOT loaded → "self-advertised via skill" was
  FALSE. **✅ DURABLE FIX LANDED (2026-06-15): rig-cli PR #11 (`link_skill_harness`) MERGED** — `rig apply`
  idempotently symlinks each enabled skill into the harness discovery dir (claude-code → ~/.claude/skills),
  harness-keyed via `harness.kind`, never clobbers real non-symlink dirs, `rig status` reports link drift.
  **Verified post-merge: 56 entries in ~/.claude/skills, 53 symlinks all resolve (0 broken SKILL.md), 3
  real hand-authored dirs untouched.** So the **cc** harness is DONE + reproducible for any machine.
  **STILL OPEN — must work for ALL supported harnesses** (tg#3747), each with its own discovery mechanism:
  cc → `~/.claude/skills` (dir-skills); codex → `~/.codex/AGENTS.md`; opencode (oc) → `~/.config/opencode/AGENTS.md`;
  gemini → `~/.gemini/GEMINI.md`; commandcode (cmd) → has its own agent CLI (CONFIRMED by CTO) — provision its global instruction dir;
  pi → **earendil-works/pi** (github.com/earendil-works/pi, the minimal extensible coding-agent harness; loads AGENTS.md, multi-provider) → AGENTS.md-style provisioning like codex/oc (confirm its global dir at impl). The **supported-harness list must be in the README** (agent-tools + rig).
- **⚠️ agent-hooks don't FIRE in Claude Code — need an agents-hooks/v1 → CC bridge** (agent-tools#18): CC runs settings.json hooks (PreToolUse/PostToolUse), NOT the ~/.claude/hooks/*.json `agents-hooks/v1` descriptors rig installs → block-raw-pr-merge / block-secrets / format-on-write / etc. are ALL INERT in CC (files nothing invokes). The 'safe because guards intercept' pillar is FALSE until a bridge runner is wired into settings.json (rig-installed, harness-keyed) + verified by the clean-room e2e. Same class as the skill-loading gap. DO NOT claim any guard 'works' in CC until this lands.
  **🚧 IN-FLIGHT (2026-06-15):** dispatched a subagent (worktree) to build the dispatcher (verify CC's
  actual block contract: exit 2 vs JSON deny → map from `agents-hooks/v1` exit-10), wire it via rig into
  settings.json PreToolUse/PostToolUse, and prove a real block with a clean-room test (guard BLOCKS / benign
  PASSES). PRs pending; will NOT be claimed "works" without the clean-room proof.
- **Clean-room / Docker e2e for rig** (tg#3745, rig-cli#10): the manual symlink fixed THIS machine; only a
  fresh-environment e2e (Docker container or throwaway `$HOME`) running `rig init` as a brand-new user
  proves it works for ANYONE on ANY machine — assert skills discoverable by the harness (~/.claude/skills
  resolve + in the Skill-tool list), hooks/dispatcher/CI/auto-mode installed, idempotent re-apply,
  `rig status` clean. (Current rig tests use a tmp-$HOME unit sandbox; this is the full clean-room integration.)
- **Slim `~/.claude/CLAUDE.md` to PERSONAL-ONLY** (#3704): it should hold only user-specific content
  (identity / "address me as Alex" / preferences / dictionary / Telegram report style). ALL universal
  tool + process docs (how to use `review`/`tg`/`draw`, the no-short-timeout rule, etc.) must
  self-advertise via SKILLS, not be hand-written there — many already have skills + the `<!-- skill:X -->`
  blurbs are the self-advertising mechanism. The hand-written "## Review and sanity checks" section
  duplicates the `review` skill → remove it; audit the rest the same way. (My in-place "migrate CLAUDE.md
  review docs to subcommands" was wrong — those docs shouldn't live in CLAUDE.md at all.) Careful pass,
  it's the global config. (alex-mextner/agent-tools#15)
- **Decisions-as-buttons + the hanging-question DANGER** (#3706): verify the agent
  question-with-options flow (tg inline tappable buttons) actually works end-to-end. **Critical
  bug risk:** `tg-ctl` injects inbound messages into the agent's tmux pane via `send-keys`; if an
  interactive prompt/question is open in that pane, the injected text is typed INTO the prompt and
  LOST/corrupts it. Fix: answers must come through a SEPARATE channel (tg inline buttons → routed
  reply), and while a question is pending tg-ctl must DEFER/queue inbound text injection (detect the
  pending-question state) rather than blast it into the pane. Test + fix. (alex-mextner/tg-cli#30)
- **tg-cli `tg#<id>` message-ref convention** (tg#3715): reference inbound TG messages as `tg#3715`
  (NOT bare `#3715` — collides with PR/issue refs). The inbound inject wrap should render the id as
  `tg#<id>`; the autolink layer must recognize `tg#\d+` as a message reference and link it, **before**
  the PR/issue `#\d+` detection runs. ("— reply via tg" suffix already removed, tg-cli main 83ac5db.)
  (alex-mextner/tg-cli#28)
- **tg-cli file-excerpt attachment** (tg#3715): when a message gives a file **path + line / line-range**
  (in any of the common formats — `path:42`, `path:10-20`, `path#L10-L20`, etc.), attach the actual
  excerpt (the referenced lines) as a quote below. Currently broken — fix. (alex-mextner/tg-cli#29)
- **tg-cli `/new` command** (tg#3717): `/new [<model>] [<dir>] name [<task description>]` — spawn a new
  agent session. **Omitted options are chosen interactively via inline buttons.** Directory options =
  the dirs already in use + their `..` parents, all normalized + uniq + ranked LRU/MRU. `name` validated
  with a **uniqueness warning**. (Pairs with the decisions-as-buttons work.) (alex-mextner/tg-cli#27)
- tg-cli #25: fix-or-justify the 8 CodeQL TS findings (length-counting helpers + install TOCTOU).
  (alex-mextner/tg-cli#31)
- tg-cli AGENTS stale "~578 tests" → 997. (alex-mextner/tg-cli#32)
- hyper-saas AGENTS.md "further-trim candidates" (Systematic Debugging / Dead-code / On-Task-Completion
  / Naming / Codex-Specific) — CTO to confirm before trimming (generic blocks now covered by skills).
  (No tracking issue — hyper-saas is a product repo, not a tool repo, and this is CTO-gated.)
- Billing: Fireworks/Fire Pass (`glide` account) suspended — only that provider is down; rest work.
  (No issue — account-status note; the dead-provider dependency is removed by alex-mextner/review-cli#25.)

### 10. Third-party skill/tool ecosystem: enable · harmonize · delineate (RESEARCH) (tg#3754)
**Problem (CTO observation):** parse a month of hyper sessions and **most installed third-party skills/tools
were never invoked.** Same root cause as the skill-loading + hook-firing gaps — *installed ≠ discovered ≠
applied.* We've accreted overlapping tool systems with no routing doctrine → agents default to grep/Read and
ignore the specialized tools, or burn tokens choosing between redundant ones. Need a data-driven triage:
which to enable, which to harmonize, which to delineate, which to prune.

**Full inventory of third-party / external systems to triage (NOT our own ecosystem):**
- **MCP servers (tool providers):**
  - **Haft** (`h-reason`) — structured engineering reasoning, decision artifacts, FPF patterns. Heavy ceremony.
    Overlaps superpowers brainstorming + the `h-reason` skill dir.
  - **superpowers** — skill-discovery framework + workflow skills (using-superpowers, brainstorming, debugging,
    TDD…). Self-asserts at SessionStart.
  - **sverklo** — multi-repo code intelligence: semantic search, concepts/clusters, memories, impact/deps,
    review-diff. Overlaps serena + grep + the `semantic-code-search` skill.
  - **serena** — LSP-backed semantic code: symbol find/edit, references, onboarding, project memories.
    Overlaps sverklo + grep.
  - **context7** — live library/framework docs. Overlaps WebFetch + agent-browser.
  - **computer-use** — desktop control (native apps, cross-app).
  - **claude-in-chrome** — browser automation (DOM-aware). Overlaps computer-use browser tier + agent-browser.
- **CLI-backed external skills:** **agent-browser** — read web pages as markdown (replaces WebFetch for long
  pages). Overlaps context7 + WebFetch + claude-in-chrome.
- **Hand-authored skill dirs (non-rule):** **h-reason**, **debate-swarm**, **moshi-best-practices**.
- *(Boundary, NOT the subject: our own — tg, review, draw, 3d, rig, task-cli, rtk, linear, + 50 agent-tools
  rule-skills. The research delineates the third-party tools AGAINST these.)*

**Three axes:**
1. **Enable (заэнейблить)** — audit each: actually installed + discoverable in EVERY harness we support?
   (Likely many are configured-but-inert or cc-only — same failure class as skill-loading/hook-firing.)
2. **Harmonize (подружить)** — make overlapping systems compose, not collide: Haft-vs-superpowers reasoning;
   serena-vs-sverklo memories (two stores + agent-tools MEMORY.md = THREE memory systems); two
   "think-before-build" frameworks; agent-browser-vs-context7-vs-WebFetch doc reading.
3. **Delineate (разграничить)** — a written routing doctrine / decision table so an agent picks right without
   burning tokens: code search → serena (symbol-precise) | sverklo (cross-repo concept) | grep (literal);
   docs → context7 (libraries) | agent-browser (arbitrary web); reasoning → Haft (irreversible/decision-record)
   | superpowers brainstorming (divergence).

**Evidence step (the killer):** parse ~1 month of hyper session transcripts (`~/.claude/projects/**/**.jsonl`),
count tool/skill invocations by name, surface the never-fired and rarely-fired. Output = a routing-doctrine doc
+ concrete enable/prune/route actions (likely a rig provisioning concern + a routing skill).
Tracking issue: **alex-mextner/agent-tools#19**.

**✅ Evidence DONE (2026-06-15) — hypothesis confirmed:** only **9/204 hyper sessions (4.4%)** invoked any
third-party tool; serena + claude-in-chrome = literal zero. Root cause: those tools are **deferred** (need a
ToolSearch first) so the agent stays on the zero-friction Bash+grep path. Full table + script in **#19**.

---

## 📇 Open-ticket ledger — every open issue/PR (keep in sync; nothing dropped)
*Snapshot 2026-06-15. The prose sections above carry the context/detail; THIS list is the completeness index —
if a ticket exists it must appear here. Details live in the tickets, not here.*

- **agent-tools** — #12 shared-lib (Python) · #13 research-cli · #14 harvest skills · #15 slim ~/.claude/CLAUDE.md ·
  #18 agent-hooks→CC bridge *(🚧 in-flight subagent)* · #19 third-party tool enable/harmonize/delineate ·
  **PR #17** format-on-write hook *(held until #18 bridge fires)*.
- **rig-cli** — #5 enable repo security settings · #6 auto-mode = gitignored local · #7 rollout wave-2 (bots +
  self) · #8 model-currency manifest+cron · #9 multi-harness skill provisioning (codex/oc/gemini/cmd/pi) ·
  #10 clean-room/Docker e2e.  *(#11 skill-harness-link ✅ MERGED 2026-06-15.)*
- **review-cli** — #24 all board models agentic via opencode · #25 stale `DEFAULT_MODELS` → manifest ·
  #26 propagate canonical ecosystem one-liner to other repos.
- **tg-cli** — #26 `/tasks` · #27 `/new` · #28 `tg#<id>` ref+autolink · #29 file-excerpt attachment ·
  #30 decisions-as-buttons + inject-collision · #31 CodeQL 8 TS findings · #32 stale test count (578→997).
- **3d-cli** — **PR #1** slim AGENTS.md (rollout).
- **task-cli** — #1 Phase-2 deps+Gantt · #2 daemon+webhooks · #3 completion/due notify · #4 integrations.
  *(foundation in-flight, agent a9cef4ca — check for its branch/PR.)*
- **draw-cli** — none open.

> Bridge subagent (agent-tools#18) will likely open NEW PRs (agent-tools + maybe rig-cli) — add them here when it reports.

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
