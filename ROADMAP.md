# Agent CLI Ecosystem — Roadmap

Status snapshot: **2026-06-16**. This is the handoff/roadmap for the agent-native CLI ecosystem
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
- ✅ **Auto-mode provisioned + made AUTOMATIC (self-dogfood)** — committed `rig.yaml`
  (`harness.auto_mode: true`) AND committed `.claude/settings.json` (`bypassPermissions`). Reversed an
  earlier wrong call: `.claude/settings.json` is Claude Code's SHARED/committed slot, so committing it is
  what makes auto-mode turn on by itself on a fresh checkout (gitignoring it was the reason it "didn't
  turn on" — CTO caught this). Only `.claude/settings.local.json` (the personal slot) stays gitignored.
  Matches the rig-cli reference. ⚠️ Safety caveat: the "safe because agent-hooks intercept" rationale is
  bridge-pending — CC-level guards are INERT until the #18 hook-bridge lands + is live-verified.
- ✅ **CI gates de-GHAS'd (PR #23 merged)** — the rig-shipped gates depended on PAID GitHub features and
  hard-failed on the hyper org/private repo. Dropped them for free OSS engines (CTO call, tg#3774):
  `secret-scan.yml` → gitleaks **OSS binary** (`gitleaks dir`; same engine+ruleset, no org license, vs the
  `gitleaks-action` that demands a paid `GITLEAKS_LICENSE`); `dependency-review.yml` → scripted
  `dep-audit.sh` (whole-tree bun/npm/pip/cargo/go audit, no GHAS, vs `dependency-review-action` that needs
  GHAS on private repos). READMEs corrected (the old "GHAS only for the dashboard" claim was wrong). Verified
  parity (tg#3777): secrets 1:1, vulns equal-or-better; **one gap = license-policy → #21**. CodeQL
  actions-suppressions in the leftover-grep/review-threads templates were already correct (untouched).

### hyper #463 (rig-rollout PR) — how it goes green
- The 3 red checks on #463 were all from the GHAS/licensed gates above; **PR #23 fixes them at the source**.
  #463 goes green when the **rig agent re-applies `rig apply`** (pulls the #23 templates + commits auto-mode in
  one pass) — owned by the parallel rig agent, NOT hand-edited on the #463 branch (two agents, one branch =
  collision). The CodeQL-actions red on #463 is a stale rollout copy → the re-apply replaces it.
- ⚠️ **Separate REAL finding (pre-existing, not the rollout):** hyper's OWN `security-scan.yml` "Dependency
  Audit" flags a genuine high vuln — **esbuild `^0.25` (GHSA-gv7w-rqvm-qjhr**, RCE via Deno install path; not
  exploitable on Node/bun). Bump or waive with justification — tracked as **HYP-743** (Linear).

### ➡️ DELEGATED to the rig agent — finish the hyper rollout (CTO tg#3783/#3784)
The parallel rig agent owns completing hyper's rollout; this session hands it off:
1. **`rig apply` re-run on hyper** → pulls the #23 templates (de-GHAS'd gates) + commits auto-mode → greens #463
   (replaces the stale CodeQL-actions copy too).
2. **Remove hyper's bespoke `security-scan.yml`** (CTO: common gates belong in agent-tools, not per-repo).
   Coverage parity BEFORE deleting — its three jobs: **Semgrep** ✅ covered by agent-tools `ci/sast/`;
   **bun audit** ✅ covered by `ci/dependency-review/dep-audit.sh` (#23); **Trivy** ❌ **NO agent-tools
   equivalent yet** → either add a `ci/trivy/` (or fs-scan) gate to agent-tools and provision via rig, or
   keep hyper's Trivy step. Don't silently drop Trivy. Tracked: **agent-tools #24**.
3. **esbuild HYP-743** must be bumped/waived for any whole-tree dep-audit (rig's or hyper's) to pass green.

### ecosystem hygiene
- ✅ ralphex/quorex removed everywhere + `alex-mextner/quorex` **archived** with a "superseded by
  review-cli" banner; 3d-cli docs purged; hyper-saas AGENTS.md ralphex workflow removed (PR #461 merged).
- ✅ hyperide.ai homepage URL cleared on all 6 tool repos.

---

## ✅ Done (2026-06-16) — auto-mode SAFETY BLOCKER CLOSED + 17 PRs landed
*(The detail below lists the first 9; the §3 lib stack #30/#33/#17, this roadmap #40, rig-cli #18, and the
3 cross-repo doc PRs (review #27, draw #4, 3d #7) landed later the same session — see "Next actions".)*

### 🎯 Auto-mode guard — RESOLVED (the central "bridge-pending safety" blocker is CLOSED)
- **#20 hook-bridge MERGED** (`d4ff40f`) + hardened (timeout_ms null/bool/0/neg; non-UTF-8 decode
  pinned `errors=replace`/`encoding=utf-8`; NotebookEdit path normalized so pre-write guards scope it).
- **`rig apply` wired `cc_hook_bridge` into `~/.claude/settings.json`** (3 dispatcher hooks:
  PreToolUse×2 + Stop). **Live-proven THIS machine:** a blockable cmd → `permissionDecision: deny`,
  benign → allow. So `defaultMode: auto` is now GUARDED — the installed agent-hooks (block-secrets-write,
  enforce-timeout-on-bash, require-review-before-commit, block-no-verify, block-raw-pr-merge, …) actually
  FIRE under auto-mode. The roadmap's "safe pillar is bridge-pending" caveat is now satisfied.
- Provisioned via rig (dogfood), not hand-edits.

### Merged this session (gh ship, gated — green CI + resolved codex threads)
- **agent-tools #38** — NEW `ci/tests` slot (pytest via uv, secretless) + rig provisions it. agent-tools had
  12 governance CI slots but none that RAN the repo's tests → `gh ship` refused no-CI PRs. This unblocked the
  whole backlog. Keystone.
- **agent-tools #29** — `lib/agenttools_retry` (§3 "retry"). **agent-tools #32** — mcp-policy (review is a
  CLI+skill, NOT an MCP; removed the dead `mcp/review` slot + repointed 6 dangling refs). **agent-tools #34**
  — third-party tool triage (#19). **agent-tools #35** — strict-ticket-discipline skill + require-ticket
  hook (task-cli integration piece; fixed 2 real hook bugs: honor `git -C`, don't exempt on flags in the msg).
- **rig-cli #21** — NEW `rig stats show`: tool-adoption analytics across agent harnesses (sources/aggregate/
  render pipeline, json/tui/web). Fixed 2 P2 (parse_iso non-string; inverted --since/--until).
- **review-cli #28 + #29** — spec-web phase-1 (mobile UX) + phase-2 (submit→agent handoff, debounced disk
  drafts, `review spec-web reply` → UI+tg). §7 review-cli follow-ups. (Caught a CI regression the building
  agent's subset-test run missed — `--spec` in `_VALUE_TAKING_OPTS` broke a guard test.)

### Durable / infra
- Global `~/.gitignore` now ignores `.serena/` + `.claude/worktrees/` (machine artifacts were tripping
  `gh ship`'s clean-worktree check — not a ship bug; ship works fine from clean worktrees, verified).
- `gh ship` (ci/ship/ship.sh) confirmed repo-agnostic: ran it against rig-cli/review-cli via `bash
  <agent-tools>/ci/ship/ship.sh <PR>` from each repo's worktree.

### Still-open in §3 (Shared lib extraction) — handed off with documented threads
- **#30 lib-config** (§3 "config") — rebased onto main, conflict resolved, 30 tests pass (with pyyaml). **2
  open P2:** (1) `core.py` "global-only" mode still loads the repo layer (no suppress) — needs an API knob +
  test; (2) ⚠️ **SYSTEMIC:** umbrella `lib/pyproject.toml` builds only `agenttools_log`
  (`include=["agenttools_log*"]`), so NO new lib module installs — fix ONCE for all (`#29 lib-retry` already
  merged with this latent gap). 
- **#33 lib-advertise** (§3 "advertise") + **#17 format-hook** — CONFLICTING; need rebase (lib/README.md row
  conflict, same as #30) + thread resolution.
- **Uncommitted lib modules** in `.claude/worktrees/wf_9d942e4d-*`: `tmux_inject` (§3 "tmux-inject"),
  `daemon` (§3 "daemon-supervisor"), `registry` (§3 "registry"); `gantt`/`providers` committed → need push+PR.

### New tickets opened (add to ledger)
- **agent-tools #39** — surface advisory (exit-0) v1 hook messages as CC `additionalContext` (bridge follow-up).
- **review-cli #30** — spec-web draft autosave race → server-side ordering token/tombstone.

## 🎯 Next actions — audit-reconciled 2026-06-16 (ordered; drives the loop)
*Live cross-repo audit of all 7 tool repos (roadmap-state-audit workflow). Re-audit (or `gh pr view`)
before acting — items 1–4 + 6's #18 LANDED later in this same 2026-06-16 session.*
1. ✅ **DONE — Easy wins:** review-cli **#27**, draw-cli **#4**, 3d-cli **#7** MERGED; review-cli **#26**
   closed. PENDING: tg-cli **#33** (no local checkout — clone tg-cli first), 3d-cli **#1** (blocked on a
   required check — re-check).
2. ✅ **DONE — Cleanup:** agent-tools **#18** closed (bridge done, pointed at `d4ff40f`); rig-cli **#12**
   was already merged.
3. ✅ **INVESTIGATED — not a hard blocker:** the lib modules DO install via their NESTED pyproject
   (`uv --with .../lib/agenttools_config` — verified). The umbrella `lib/pyproject.toml` is just
   agenttools_log's own dist; the real fix is per-module README install paths (done in #30). No packaging
   refactor needed. (Optional polish: clarify the misleadingly-named root pyproject.)
4. ✅ **DONE (PRs) — §3 lib stack:** **#30** lib-config, **#33** lib-advertise, **#17** format-hook ALL
   MERGED (each had REAL codex P2s — global-only, symlink-write-through, ruff-in-Black-repo — fixed+tested).
   ⏳ STILL TODO: **proper commit+push+PR** the 3 uncommitted modules (tmux_inject/daemon/registry) — they are
   **backed up** at `~/xp/agent-tools-lib-modules-backup-2026-06-16/` (loss risk gone) but NOT yet in git;
   plus PR the committed gantt/providers. Do BEFORE any `git worktree prune` of `wf_9d942e4d-*`.
5. **tg-cli (S):** ship #34 (autolink → tg#28) and #35 (defer-while-waiting → tg#30) after clearing 1 thread each. (No local tg-cli checkout — clone first.)
6. **rig-cli:** ✅ **#18** (readme) MERGED. ⏳ **#20** (`rig config get|set`, §5 piece) — has a real P2 (validate
   the GLOBAL layer in isolation so a repo overlay can't mask a bad `--global` write; fix verified-viable: an
   empty-cwd `_load_plan` sees only the global layer) + a `docs/gen_svgs.py` rebase conflict to resolve.
7. **rig-cli §5 (L):** repo-settings provisioning via `gh api` + `agent-browser` (current branch
   `roadmap-rig-repo-settings`). Guard with capability detection (org/private repos can hard-fail on
   ruleset/GHAS endpoints — same class that forced the GHAS→OSS retreat); default-on only where the API permits.

## 🚧 In-flight at handoff (background agents — verify state, resume if incomplete)

> ⚠️ **SUPERSEDED (2026-06-16):** every "hook-bridge / auto-mode-guard is bridge-pending / live-verify
> pending / land #20 / #18 is a hard blocker" item in THIS section and below is **DONE** — see
> "✅ Done (2026-06-16)" above. #20 is merged (`d4ff40f`), `cc_hook_bridge` is wired into
> `~/.claude/settings.json` and **live-proven** (deny/allow). Do NOT re-verify or re-merge the bridge.
> The `.claude/scripts/pr-ship.sh` shim is present and CI exists (#36/#37/#38). Follow the "🎯 Next
> actions" block. The ONLY still-open auto-mode item is the **hyper product repo** rollout (below) —
> a different repo, not the bridge.

### ➡️ rig-tooling session (2026-06-15) — RESUME HERE
- **hook-bridge #18 LIVE-CC VERIFIED** ✅ — fresh `claude -p` under `bypassPermissions`: a raw
  `gh pr merge` → CC `permissionDecision: deny` (bridge → block-raw-pr-merge), a benign cmd → pass.
  The "live-CC round-trip" the bridge section flags as pending is **done**. #20's conflict with main is
  resolved + pushed (branch `cc-hooks-bridge`). **Land #20 then #12.**
- **Auto-mode finding: `auto` > `bypassPermissions`, and `auto` is USER-level ONLY** — CC silently ignores
  `defaultMode:auto` in a repo's project/local `.claude/settings.json` (since v2.1.142; confirmed empirically
  on 2.1.177), honoring it only from `~/.claude/settings.json`. `auto` (research preview) auto-approves WITH a
  safety classifier; bypass skips everything. So the committed-per-repo bypass rollout is the inferior model
  AND project-bypass overrides user-auto. Redesign: `docs/specs/2026-06-15-harness-layer-redesign.md` (PR #25)
  — CC → user-level `auto`, migrate the 6 tool repos off committed project bypass, per-harness branch-on-kind.
- **Open PRs:** **#25** (harness-redesign spec), **#26** (this rig-config-UX capture: config get/set, schema,
  symlinks, repo-settings), **#20** + **rig-cli #12** (the verified bridge). agent-tools `main` is now
  branch-protected → PRs only (the `gh ship` script `.claude/scripts/pr-ship.sh` was missing here — fix it).
- **Pending impl:** delete `rig setup` alias (CTO); implement the #26-captured rig features; verify tg
  `PermissionRequest` passthrough fires in `auto` mode (tg-ctl already installs the hook).
- **⚠️ `/Users/ultra/work/hyper-canvas-draft` auto-mode STILL DOES NOT WORK (CTO confirmed 2026-06-15).**
  Diagnosed: its committed `.claude/settings.json` + `.claude/settings.local.json` set **no `defaultMode` at
  all** (the huge `permissions.allow` list is auto-accumulated "always allow", not auto-mode). And `auto`
  can't be project-committed (user-level only), so the real fix is **user-level `auto`** (or the rig harness
  migration), provisioned by **rig** — NOT a hand-edit (a manual settings.local.json edit was made then
  reverted per "fix tooling, not manual"). Close it via the rig harness redesign + the hyper rollout
  (delegated to the rig agent). It's a TEAM product repo → user-level auto for Alex, don't commit bypass to
  the org repo.
- task-cli foundation (agent a9cef4ca) — building the Python tool in the new `alex-mextner/task-cli`
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
- **rig provisions REPOSITORY SETTINGS at init/apply** (config-driven, sensible defaults ON) (#3696/#85,
  CTO 2026-06-15): rig.yaml declares the repo's GitHub settings and `rig init`/`rig apply` reconciles them,
  the same way it does skills/hooks/CI. **Two backends, auto-selected per setting:**
  - **`gh api`** for everything the GitHub API exposes: **branch protection / rulesets** (require-PR,
    required status checks + reviews, linear history, block force-push — the `…/settings/rules/` the CTO
    just enabled by hand), **GHAS** (Dependency Graph + vuln-alerts + secret-scanning + code-scanning),
    merge-button policy (squash-only, auto-delete head branch, auto-merge), Actions permissions, etc.
  - **`agent-browser`** for settings the API does NOT expose — drive the GitHub settings UI headlessly to
    flip the switches `gh api` can't reach. A first-class rig backend invoked INSIDE init/apply, not a
    manual step.
  Everything maps onto rig.yaml (a `repo:`/`github:` block) with the **secure/sensible set enabled by
  default**, so a fresh `rig init` lands the guardrails (branch protection + GHAS + squash-merge + ship gate)
  with zero hand-toggling. `rig status` reports drift on these like any other managed artifact. (Supersedes
  the narrow "gh api enable Dependency Graph" scope.) (alex-mextner/rig-cli#5)
- **`rig config get|set [--global] <path> [val]` — the recommended way to change config** (CTO 2026-06-15):
  read/write a single nested key in `rig.yaml` (or `~/.config/rig/config.yaml` with `--global`) WITHOUT
  hand-editing YAML, and **reconcile immediately** — `set` runs `rig apply` so the change takes effect on the
  spot. `<path>` is dot-notation into the YAML tree (`harness.mode`, `ci.items.secret-scan.tier`,
  `github.branch_protection.required_reviews`). The tooling-driven config-change UX: agents and the CTO change
  rig config through `rig config set`, never by hand-patching the file. (Pairs with the repo-settings
  provisioning above + the harness-layer redesign + the "fix tooling, not manual" rule.)
- **rig.yaml has a real, ENFORCED JSON schema + config docs in the rig repo** (CTO 2026-06-15): ship a JSON
  Schema for `rig.yaml` + the global config and actually VALIDATE against it (not just prose) — `rig apply` /
  `rig config set` reject an unknown key or bad value with a clear error + the schema path, and editors get
  completion/validation. The human-readable config reference (`docs/config-schema.md`, already cited from the
  rig.yaml header) must EXIST in rig-cli and stay in sync with the schema (one source of truth, every key
  documented). "It should work" = a malformed config fails loudly, not silently.
- **rig always provisions `AGENTS.md` + `CLAUDE.md` as symlinks** (CTO 2026-06-15): a repo should carry BOTH
  files so every harness finds its expected name (codex/opencode/gemini read `AGENTS.md`, Claude Code reads
  `CLAUDE.md`), pointing at ONE source of truth via a symlink (e.g. `CLAUDE.md` → `AGENTS.md`) so the two can
  never drift. `rig init`/`rig apply` create/repair the symlink (never clobber a real file without backup);
  `rig status` flags a missing or broken link.
- **Auto-mode is COMMITTED** (decision REVERSED 2026-06-15, CTO): `.claude/settings.json` is Claude
  Code's SHARED/committed slot — committing it (`defaultMode: bypassPermissions`) is what makes auto-mode
  turn on by itself on every checkout. The personal per-machine slot is `.claude/settings.local.json`
  (that one stays gitignored). rig-cli#6 ("gitignore settings.json") is REVERSED/wontfix — the opposite is
  correct. ⚠️ The footgun every reviewer flagged: committed `bypassPermissions` auto-accepts every tool
  call on any LOCAL CLI checkout (web sessions ignore it), and the agent-hooks that would gate it are
  INERT in CC until the #18 bridge lands + is live-verified. So the "safe" pillar is bridge-pending —
  #18 is now a HARD blocker for blessing auto-mode, not a nicety. (alex-mextner/rig-cli#6 — reverse it.)
- **Rollout (#3686)**: wave-1 tool repos = PRs open (draw/3d green, tg pending TS fix). **Wave-2 =
  bots** (ExpenseSyncBot, garage-band, summary-bot, esphome-ir, claude-p, diploma, sme-archiving-gc,
  talks, upwork) + review-cli/rig-cli/agent-tools themselves. Each: `rig init --yes` + conservative
  AGENTS/CLAUDE slim (drop now-self-advertised generic rules, keep project specifics) + harvest report.
  (alex-mextner/rig-cli#7)
  - **Auto-mode COMMITTED-settings.json status (on `origin/main`, 2026-06-15):** rig-cli ✅, agent-tools ✅,
    review-cli ✅, task-cli ✅, draw-cli ✅, 3d-cli ✅ — all six Python tool repos now COMMIT
    `.claude/settings.json`. draw/3d had `.claude/` blanket-gitignored (the old "keep local" call) — that
    was the ecosystem-wide cause of "auto didn't turn on"; reversed (narrowed to `settings.local.json`).
    **tg-cli** is Bun/TS → migrates to Python last; provision then. ⚠️ The rig.yaml
    `mcp.review.command: "review --mcp"` is STALE (the `--mcp` flag was dropped in the review subcommand
    refactor; the global `~/.claude/mcp/mcp.json` registration is broken too) — fix-or-remove before any
    `rig apply` registers a dead MCP server (tracked in §9 misc).

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
- Candidate skills (Sources: 3d-cli AGENTS):
  - ✅ **worktree-via-project-cli** (dep provisioning, distinct from worktree-base-trap) — authored
    (`skills/universal/`), GREEN-verified per writing-skills.
  - ✅ **queued-report-durability** (channel-unavailable → don't fake delivery) — authored, GREEN-verified.
  - ⏭️ **subagent-delegation contract** — SKIPPED: already covered by `subagent-handoff-contract` (dupe).
  - ⏭️ **"diagnostic image ≠ proof" acceptance bar** — SKIPPED: overlaps `visual-proof-cycle`; fold a
    proof-claim acceptance-bar note into that skill rather than ship a near-duplicate.
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
  **✅ BUILT (2026-06-15) — NOT merged:** **agent-tools PR #20** (dispatcher `lib/cc_hook_bridge`: reads CC's
  tool-call JSON, maps `(event,tool)`→v1 point, runs the `~/.claude/hooks/*.json` descriptors, translates
  exit-10 BLOCK → CC's `permissionDecision: deny` on exit 0 — contract confirmed against installed CC 2.1.177)
  + **rig-cli PR #12** (`register_hook_bridge` wires it into settings.json PreToolUse/PostToolUse on `rig apply`).
  Clean-room test PROVES it: raw `gh pr merge` → DENY, `gh ship` → pass.
  **➡️ NEXT ACTION (DO THIS before merging): live-CC round-trip verification.** The clean-room proof drives the
  dispatcher exactly as CC invokes it, but it was NOT run inside a live CC session. To close the loop: in a
  real CC session run `rig apply` (so `register_hook_bridge` writes the settings.json hooks), then trigger a
  guarded action (a raw `gh pr merge`) and confirm **CC itself refuses the tool call**; confirm a benign
  action passes. THEN merge **#20 first, then #12** (interdependent). Until that live round-trip passes, the
  "safe because guards intercept" pillar stays UNPROVEN — do not claim any guard "works" in CC, do not merge.
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
- **`review --mcp` is broken ecosystem-wide** (no tracking issue yet — surfaced by the auto-mode rollout
  reviewers): the review subcommand refactor dropped the `--mcp` flag, but `~/.claude/mcp/mcp.json` AND
  every rig.yaml `mcp.review.command` still invoke `review --mcp` → `rig apply` registers a dead MCP
  server (and the global review MCP is currently inert). Fix-or-remove: implement a real review MCP
  entrypoint, or drop `mcp.review` from the rig.yaml template + the global registration. Affects all
  six provisioned tool repos' rig.yaml.

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

**2026-06-16 delta:** MERGED → agent-tools #20 (bridge, +wired+live-proven), #29 (lib-retry), #32 (mcp-policy),
#34 (=#19 triage), #35 (strict-ticket), **#38** (ci/tests gate, NEW); rig-cli **#21** (rig stats, NEW);
review-cli **#28**+**#29** (spec-web). OPENED → agent-tools **#39** (bridge advisory-msg), review-cli **#30**
(spec-web autosave race). STILL OPEN → agent-tools #17 (format-hook), #30 (lib-config, 2 P2 incl. umbrella-
packaging systemic), #33 (lib-advertise). SYSTEMIC TODO → `lib/pyproject.toml` packages only agenttools_log.

- **agent-tools** — #12 shared-lib (Python) · #13 research-cli · #14 harvest skills · #15 slim ~/.claude/CLAUDE.md ·
  #18 agent-hooks→CC bridge *(✅ built: PR #20 + rig-cli #12, clean-room-proven; live-CC round-trip pending)* ·
  #19 third-party tool triage · #21 OSS license-policy gate · #22 self-hosted security dashboard ·
  #24 Trivy CI gate · **PR #17** format-on-write hook · **PR #20** hook-bridge dispatcher
  *(merge AFTER live-CC verify; before rig-cli #12)*.
- **rig-cli** — #5 enable repo security settings · #6 auto-mode = gitignored local · #7 rollout wave-2 (bots +
  self) · #8 model-currency manifest+cron · #9 multi-harness skill provisioning (codex/oc/gemini/cmd/pi) ·
  #10 clean-room/Docker e2e · **PR #12** hook-bridge provisioning *(depends agent-tools #20)*.
  *(#11 skill-harness-link ✅ MERGED 2026-06-15.)*
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
- **auto-mode `bypassPermissions` = COMMITTED `.claude/settings.json`** (CC's shared slot → turns on by
  itself; `.claude/settings.local.json` is the gitignored personal slot). [Reversed 2026-06-15.] Its
  "safe because guards fire" rationale is **RESOLVED 2026-06-16** (#18/#20 bridge merged + wired into
  `~/.claude/settings.json` + live-proven; guards fire under auto-mode). No longer bridge-pending.
- **Use `gh ship`, not raw `gh pr merge`** (the `block-raw-pr-merge` guard enforces it — now LIVE via the bridge).
- Tool repos work directly on `main` (push often); hyper-saas via PR + `gh ship`.
- This roadmap lives in `agent-tools` because ecosystem work should run from a tool repo / agent-tools,
  not from an unrelated product repo.

---

## 🆕 From the GHAS-parity analysis (2026-06-15, tg#3777/#3779/#3780)

We dropped every GHAS / licensed-action gate for free OSS equivalents (gitleaks OSS **binary** instead of the
licensed `gitleaks-action`; scripted `dep-audit.sh` instead of `dependency-review-action`). Verified parity:
secret-scan is **identical** (same engine + `useDefault` ruleset), dep-audit is **equal-or-better** for vulns
(whole-tree vs PR-diff). Two follow-ups the parity check surfaced:

- **OSS license-policy gate — agent-tools #21.** The one real gap: `dependency-review-action` also enforced a
  license allow/deny policy, which `dep-audit.sh` does not. Fill with an OSS license checker
  (`license-checker` / `cargo-deny` / `pip-licenses`), rig-provisioned as a CI gate, default-deny copyleft
  (AGPL/GPL) to match the old template. Lives in `ci/license-policy/`.
- **Self-hosted security findings dashboard — agent-tools #22.** The one thing GHAS uniquely offered that we
  don't replicate is its hosted code-scanning dashboard. Aggregate the OSS scanners' SARIF/JSON (CodeQL-CLI,
  Semgrep, gitleaks, dep-audit) into one Tailscale-served dashboard — likely **extending review-cli's existing
  dashboard** (SARIF is the common format). CTO: copy GitHub's code-scanning UI and improve it (cross-repo
  view, our own triage state, link to the suppressing `// codeql[...]` line).
