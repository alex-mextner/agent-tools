# Shared-library architecture for `agent-tools/lib`

Status: DESIGN (no code). Author: architecture pass per CTO #3676.
Scope: turn `agent-tools/lib` from one module (`agenttools_log`) into the real
foundation every CLI in the ecosystem is built on, so new tools spin up fast on
one uniform architecture.

The CTO's brief (#3676), unpacked into the seams to generalize:

> module/feature systems, adapters, model-provider support layers (with failover
> and fallback chains), config + options, hook systems (git / harness / our-tools),
> tool self-advertising, and anything else genuinely shared.

This doc does three things: (1) catalogues each named pattern with `file:symbol`
evidence and a duplication count; (2) proposes the `lib/<module>` layout; (3)
resolves the Python-vs-TypeScript split and gives a phased extraction plan.

---

## 0. The tools in scope (and their language)

| Tool | Lang | Lib dir | What it is |
| --- | --- | --- | --- |
| `review-cli` | Python | `reviewlib/` | Multi-model review + AI panels. The richest tool: provider board, failover, opencode routing, per-project module registry, mode dispatch, config cascade, skill install. |
| `rig-cli` | Python | `riglib/` | Repo/dev-machine provisioner. Config cascade, catalog→plan→apply over the agent-tools umbrella. |
| `tg-cli` | **TypeScript/Bun** | `features/` | Telegram bridge. Feature-split modules, config `.env`, install-skill, hooks (`agents-hooks/v1`), render, transport. |
| `draw-cli` | Python | (`/Users/ultra/xp/draw-cli`) | Image gen. Has its own install-skill (4th copy). |
| `agent-tools` | mixed | `lib/` | The umbrella: `lib/agenttools_log`, `skills/`, `agent-hooks/` (`agents-hooks/v1`), `git-hooks/`, `ci/`, `mcp/`. |

Future consumers the CTO named: `task-cli` (Python — a classifier that wants the
provider layer), a future `research-cli`. Both want model-provider support.

Two languages is the central constraint, addressed in §9.

---

## 1. Module / feature system

**What it is.** A self-describing unit (a "feature", "module", "mode") that a tool
discovers, trust-gates, and dispatches to — instead of hard-wiring a switch
statement. Three independent implementations of the same idea exist today.

**Evidence.**

- **review-cli per-project visual-module registry** — the most complete instance.
  - Contract: `reviewlib/features/visual/module_api.py:VisualModule` (a
    `runtime_checkable` Protocol: `name`, `activates(ctx)`, `cv_check`,
    `vision_questions`, `judge`).
  - Discovery + trust + load: `reviewlib/features/visual/registry.py` —
    `discover_specs()` (project manifest `<project>/.review/visual-modules.json` +
    a global registry `~/.config/review-cli/modules.json`), `load_modules()`
    (discover → trust-gate → `importlib` load → Protocol-conformance check),
    `trust_module()` / `register_module()` (the `review trust-module` /
    `review register-module` verbs).
  - Trust model: `_trust_state_for()` — **trust-by-default**, with an opt-in
    `REVIEW_UNTRUSTED_MODULES=1` TOFU quarantine + `sha256` entry pin +
    `activates_on` pin, and an append-only audit at
    `~/.cache/review-cli/visual/modules-audit.jsonl` (`_audit()`).
  - Manifest schema key: `registry.py:REVIEW_API = "review-visual/v1"`.
- **review-cli modes-as-plugins** — `reviewlib/modes/` (`review.py`, `quorum.py`,
  `brainstorm.py`, `just_ask.py`), dispatched from `cli.py`. A mode is a
  self-contained dir with a uniform entry signature; adding `modes/<x>.py` adds a
  mode. This is the *internal* (in-tree) flavour of the same registry idea.
- **tg-cli feature split** — `features/<feature>/` (15 feature dirs: `auto-attach`,
  `autolink-prs`, `render`, `transport`, `tg-ctl`, `install-skill`, `hooks`, …).
  Convention-only today (no registry object), but the same "one dir = one
  self-contained capability" shape.

**Duplication count:** the *concept* appears 3× (review visual-modules, review
modes, tg features); the **trust-gated dynamic-load registry** is implemented
**twice in full** — once in `registry.py` (Python, visual modules) and once as the
hook trust gate in `tg-cli/features/hooks/runner.ts` (TS) — see §5. Those two are
the same algorithm (discover → sha-pin → trust-by-default → audit jsonl) applied to
different payloads.

**Generalized lib module: `lib/registry`.** A *self-describing module + manifest +
trust-gated loader*, payload-agnostic:

- A `ModuleManifest` schema: `{ api: "<tool>/<vN>", modules: [{ name, entry,
  runtime, activates_on[], description }] }` at a well-known project path
  (`<project>/.<tool>/modules.json`) plus a global registry file.
- `discover(project) -> [ModuleSpec]`, `load(specs, contract) -> ([Loaded],
  [Quarantined])` where `contract` is the consumer's Protocol/interface
  (review passes `VisualModule`; a future tool passes its own).
- The **trust kernel** factored out: `trust_state(spec, store, guard_env)`,
  `sha256` entry pin, `activates_on` pin, append-only `audit.jsonl`, the
  trust-by-default default + opt-in `<TOOL>_UNTRUSTED_MODULES` guard. This kernel
  is **shared with `lib/hooks`** (§5) — both are "run a trusted dropped-in
  descriptor", so the trust kernel is one module, not two.

Each tool keeps its own *contract* (the Protocol the modules implement is
domain-specific); the lib owns *discovery, trust, audit, dispatch ordering*.

---

## 2. Adapters

**What "adapter" means across the tools.** It is overloaded; three distinct
adapter seams exist, and only some are shared.

- **Backend/provider adapter** (review) — `reviewlib/backends.py`:
  `resolve_backend(model) -> Callable[..., ReviewResult]` maps a model string to a
  `review_codex` / `review_gemini` / `review_zai` / `review_commandcode` /
  `review_claude` / `review_opencode` function. Each backend is an adapter from a
  uniform `(model, prompt, diff, cwd, timeout) -> ReviewResult` call onto a
  concrete transport (CLI exec vs OpenAI-compatible HTTP). **This is the
  provider-layer adapter — §3 owns it.**
- **Transport adapter** (tg) — `features/transport/telegram.ts` (Telegram Bot API)
  vs `features/render/*` (message shaping). A tool-specific delivery seam; not
  shared (only tg sends Telegram).
- **Catalog/carrier adapter** (rig) — `riglib/catalog.py` adapts the agent-tools
  on-disk layout into a flat item registry, and `riglib/actions/runner.py` adapts
  a plan item onto a filesystem install action. Rig-specific.

**Conclusion.** "Adapter" is NOT one shared abstraction. The only adapter seam
worth generalizing is the **provider/backend adapter** (§3). The transport and
catalog adapters stay per-tool. Calling them all "adapters" in one lib module
would be a false generalization — flagged explicitly so we don't build it.

The one cross-cutting *shape* worth standardizing is the **uniform backend
callable + a `resolve()` dispatcher keyed by a string** (review's pattern). That
shape ships as part of `lib/providers` (§3), not a separate `lib/adapters`.

---

## 3. Model-provider support layer — the biggest shared asset

**What it is.** A board of model providers with: a uniform call surface; a
CLI-vs-HTTP transport split per provider; per-provider key resolution; a
priority-ordered **availability failover** (skip a dead seat, promote a reserve);
a per-run **fallback chain**; and **opencode (`oc:`) routing** to drive any
provider agentically. Today this lives entirely inside review-cli and nowhere
else — but `task-cli`'s classifier, a `research-cli`, and even tg's planned
"classify inbound" hook all want it.

**Evidence (all `reviewlib/`).**

- Uniform call surface: `backends.py:ReviewResult` (frozen dataclass) +
  `resolve_backend(model)` (the string→callable dispatcher).
- Transport split: `backends.py:resolve_backend_mode(name, supported, default)` —
  reads `REVIEW_<NAME>_MODE`, validates against the provider's supported modes
  (`cli` / `api`), errors loudly on an unsupported forced mode.
- OpenAI-compatible HTTP backends: `backends.py:_openai_compatible_request(...)`
  (one request builder shared by z.ai + commandcode), `_parse_openai_choice`
  (tolerant parse, incl. reasoning-model `reasoning_content` fallback),
  `_parse_openai_usage`.
- Key resolution cascade: `backends.py:GEMINI_ENV_FALLBACKS` + the `.env`-file
  fallback reader around `:246` (`COMMANDCODE_API_KEY`/`ZAI_API_KEY`/etc resolved
  from env then fallback `.env` files).
- Availability probe: `backends.py:backend_available(model)` (cheap per-provider
  reachability: key present / CLI on PATH / forced-mode runnable).
- The board + priority + failover: `config.py:BoardReviewer`, `DEFAULT_BOARD`
  (8 seats, priority-ordered), `select_pool()` / `split_pool_reserve()` (startup
  failover), and `panel.py:run_board_with_failover()` (mid-run failover: a seat
  whose result is not usable is replaced by the next-priority reserve until N
  usable verdicts or the reserve is exhausted → `FailoverOutcome.degraded`).
- opencode (`oc:`) agentic routing: `backends.py:review_opencode` +
  `_opencode_runs_in_repo()` / `_ensure_opencode_readonly_agent()` — drives ANY
  provider (`oc:fireworks/...`, deepseek/kimi/qwen/glm) agentically through one
  read-only opencode agent, with a privilege-escalation guard against a repo that
  ships its own `.opencode/` config.

**Duplication count:** today **1** full implementation (review). But it is the
single most-wanted asset — `task-cli` (Python), `research-cli` (Python, future),
and tg's classify hook (TS) all want "call a cheap model, with failover, keyed by
a provider string". So the leverage is high even though the duplication count is
currently 1 — extracting it *prevents* the 2nd, 3rd, 4th copy.

**Generalized lib module: `lib/providers`.** The provider layer, decoupled from
"review" semantics:

- `Result` (rename `ReviewResult` → a neutral `ProviderResult`:
  `{ model, command, returncode, stdout, stderr, usage? }`).
- `resolve(model) -> Backend` and the backend protocol
  `Backend(model, prompt, context, cwd, timeout) -> ProviderResult` (review's
  `diff` becomes a generic `context` string — a classifier passes the text to
  classify, review passes the diff).
- The transport kernel: `resolve_mode()`, `openai_compatible_request()`,
  `parse_openai_choice/usage()`, the env/`.env` key cascade.
- The provider catalog as **data, not code**: providers (codex/gemini/claude/
  z.ai/commandcode/opencode/fireworks) declared in a table the consuming tool can
  extend. review's `MODEL_ALIASES` + `DEFAULT_BOARD` become a *default* the tool
  may override via config.
- The failover engine: `Board`, `select_pool`, `split_pool_reserve`,
  `run_board_with_failover`, generalized so a single-pick consumer (a classifier
  wanting "one cheap model, fall over if dead") and a panel consumer (review's
  4-of-8) share the same primitives. A classifier uses `pool=1` over a
  cheap-first board; review uses `pool=4`.

**Where the shared provider layer lives given Python AND TS both want it.** This
is the crux. See §9: the **provider layer is the one module that gets a real twin
implementation** (`lib/py/providers` + `lib/ts/providers`) sharing a
**language-agnostic provider/board manifest** (`lib/contracts/providers.schema.json`
+ a default `board.yaml`). The board, aliases, and per-provider endpoint/key-var
tables are *data* both runtimes read; the failover algorithm is small and twinned;
the agentic `oc:` route is twinned (both can shell out to `opencode`). A classifier
in Python imports `lib/py/providers`; tg's classify hook in TS imports
`lib/ts/providers`; both produce identical routing because the board data is one
file.

---

## 4. Config + options

**What it is.** A config cascade (global → per-repo/per-tool, location-scoped, deep
merge), env-var precedence, `~/.config/<tool>/` conventions, and flag/option
parsing.

**Evidence.**

- **rig** — the cleanest cascade: `riglib/config.py:load()` (global
  `~/.config/rig/config.yaml` → per-repo `rig.yaml`, `_deep_merge` with
  lists-replace-wholesale), `LoadedConfig` (carries layer provenance),
  `validate()` (fail-closed schema: unknown keys, enums, versions). XDG-aware
  (`global_config_path()` reads `$XDG_CONFIG_HOME`).
- **review** — `config.py:load_config()` (`~/.config/review-cli/config.yaml`,
  keys `models`/`brainstorm_models`/`board`), `MODEL_ALIASES`, the board loader
  `load_board()` (the cost-safe degrade-don't-substitute validation).
- **tg** — `features/config/env.ts:loadEnv()` + `resolveConfigEnv()` — flat
  `~/.config/tg-cli/.env`, with the canonical precedence `{ ...loadEnv(path),
  ...process.env }` (file first, real env wins). Also a `config.yaml` (`voice:`
  block) read elsewhere.
- **review env/.env key cascade** — `backends.py` around `:246`
  (`GEMINI_ENV_FALLBACKS`, the multi-file `.env` fallback reader).

**Duplication count:** the **config-file-→ env precedence + `~/.config/<tool>/`
location convention** is implemented **4×** (rig YAML cascade, review YAML config,
tg `.env` loader, review `.env` key cascade). All four hand-roll: a load, a deep
merge / precedence rule, and a `~/.config/<tool>/…` path.

**Generalized lib module: `lib/config`.**

- `config_dir(tool) -> Path` (`$XDG_CONFIG_HOME` aware, `~/.config/<tool>/`),
  `cache_dir(tool)`, `state_dir(tool)` — the path conventions, one place.
- `load_cascade(layers) -> LoadedConfig` (deep merge, lists-atomic, layer
  provenance, the rig algorithm generalized; YAML or flat `.env` source).
- `resolve_env(config_env, process_env)` — the "file first, real env wins"
  precedence (tg's rule) as a primitive.
- `validate(data, schema)` — fail-closed validation hook (rig's pattern), schema
  supplied by the tool.

This is **twin-worthy** (rig+review Python need it; tg TS needs it) but the
implementation is tiny — a strong candidate for a **shared schema + thin twin**
(§9), because the *cascade rule* and *path convention* are the valuable contract,
not the 30 lines that implement them.

---

## 5. Hook systems — three kinds, ONE contract

**What it is.** The CTO named "hooks for git, harnesses, and our tools." There are
exactly three carriers, already documented in
`agent-tools/docs/carrier-decision-guide.md`, and they are NOT interchangeable.

| Kind | Fires | Lives | Contract |
| --- | --- | --- | --- |
| **git-hook** | commit/push/msg | `agent-tools/git-hooks/` (+ `global-dispatcher/`) | shell scripts, lefthook.yml |
| **harness agent-hook** | agent tool-use, mid-session | `agent-tools/agent-hooks/` | `agents-hooks/v1` |
| **our-tools hook** | a tool's own action (e.g. tg pre-send-photo) | `~/.agents/hooks/<tool>/` | `agents-hooks/v1` |

**The key finding: kinds 2 and 3 are THE SAME CONTRACT.** The harness agent-hooks
and "our tools' hooks" both speak `agents-hooks/v1`:

- The contract spec: `agent-tools/agent-hooks/README.md` — JSON-on-stdin / exit-code
  protocol, **BLOCK = reserved exit code 10** (un-corruptible), `on_error` open/closed,
  descriptor `{ id, point, cmd, args, priority, timeout_ms, on_error }`.
- The descriptors: `agent-tools/agent-hooks/<name>/<id>.<point>.json` (8 hooks:
  block-no-verify, block-secrets-write, enforce-timeout-on-bash, stop-selfcheck, …).
- tg **vendored the identical contract**:
  `tg-cli/features/hooks/types.ts` — `HOOK_API = 'agents-hooks/v1'`,
  `BLOCK_EXIT_CODE = 10`, `HookDescriptor`, `TrustPin`, `invocationDigest()`,
  trust-by-default + `AGENTS_HOOKS_TRUST=1` guard. Its own comment:
  *"This is the universal hook framework … vendored … for tg-cli."*
- tg's runner: `tg-cli/features/hooks/runner.ts` — pure orchestration (trust-gate →
  ordered spawn → exit-code/on_error resolution → audit jsonl), I/O injected.
  Used by the tg-photo hook (`run-photo-hooks.ts`); the planned tg-classify hook
  is the same point mechanism.

**The second key finding: the agent-hook trust gate and the visual-module registry
trust gate are the SAME ALGORITHM.** `tg-cli/features/hooks/runner.ts` (TS) and
`reviewlib/features/visual/registry.py` (Python) both implement: discover dropped-in
descriptors → trust-by-default → opt-in TOFU sha-pin guard → append-only `audit.jsonl`
→ inert-not-blocking on quarantine. That is **one trust kernel implemented twice in
two languages** (the §1 finding restated). It is the single highest-confidence thing
to factor out.

**Duplication count:** the `agents-hooks/v1` contract is implemented **2×** (the
agent-tools Python reference hooks + tg's TS `types.ts`/`runner.ts`). The trust
kernel is implemented **2×** (review registry Python + tg runner TS). git-hooks are
their own shell-script family (the `global-dispatcher` is already a shared
dispatcher — not duplicated).

**Generalized lib module: `lib/hooks` (+ `lib/contracts/agents-hooks-v1`).**

- `lib/contracts/agents-hooks-v1` — the **language-agnostic contract**: the JSON
  event schema, the exit-code semantics (0/10/other), the descriptor schema, the
  `on_error` policy, the trust-pin format. This is the canonical home for what tg
  currently *vendors* and agent-tools currently documents only in a README. One
  schema, two thin runtimes read it.
- `lib/py/hooks` + `lib/ts/hooks` — the **dispatcher/runner** (twinned): discover
  descriptors for a `point`, trust-gate via the shared **trust kernel** (§1/§5),
  ordered spawn, resolve decision under `on_error`, emit audit. tg's
  `runner.ts` is already the reference TS implementation (pure + injected I/O);
  the Python twin is new but small.
- git-hooks stay a shell-script family under `agent-tools/git-hooks/`; the
  `global-dispatcher` is their shared mechanism and needs no lib change. `lib/hooks`
  may host a tiny helper to *author* a git-hook that shells into an `agents-hooks/v1`
  executable (the "AI review before commit = agent-hook + git-hook" defense-in-depth
  row of the carrier guide), but the carriers stay distinct.

A new tool that wants pre-action hooks (e.g. "tg before send", "research-cli before
a paid web call") imports `lib/<lang>/hooks`, declares its hook *points*, and gets
discovery + trust + dispatch + audit for free.

---

## 6. Advertising — tool self-advertising

**What it is.** How a tool makes agents/harnesses aware it exists and what it can
do: a `SKILL.md` (Agent Skills standard), an always-on blurb injected into each
detected harness's instruction file, a `SessionStart` aggregator hook, plus the
`--help`/usage surface.

**Evidence — this is the clearest, ugliest duplication in the whole ecosystem.**

- `reviewlib/install.py` — `install_agent_skill(name, skill_md, blurb)`,
  `_detected()`, `_append_marked()` (the `<!-- skill:<tool> -->` fenced block),
  `_ensure_sessionstart_hook()`, `_HOOK_MARKER = "# agent-tools-awareness"`,
  `_HOOK_COMMAND` (the `cat ~/.agents/skills/.blurbs/*.md` aggregator).
- `riglib/install.py` — same `SKILL_NAME`/`SKILL_MD`/`install_skill` shape.
- `draw-cli` — its own copy (4th).
- `tg-cli/features/install-skill/install.ts` — a **near-verbatim TS port**. Its
  own header comment: *"Mirror of the Python implementation in
  review-cli/draw-cli (same layout, markers, and hook command) so all three tools
  register identically."* Same `HOOK_MARKER`, same `HOOK_COMMAND`, same
  `appendMarked` regex, same `ensureSessionStartHook` JSON surgery.

**Duplication count: 4** (review, rig, draw in Python + tg in TS) — and the TS one
is an admitted hand-port of the Python one, kept in sync by discipline. This is the
**canonical "they all copied it" module**: identical markers, identical hook
command string, identical `~/.agents/skills/` + `.blurbs/` layout, identical
harness-detection table (`claude`/`codex`/`opencode`/`gemini`).

**Generalized lib module: `lib/advertise`.**

- `install_skill(manifest)` where `manifest = { name, skill_md, blurb }`. Writes
  `~/.agents/skills/<name>/SKILL.md`, the `.blurbs/<name>.md`, the
  `~/.claude/skills/<name>` symlink, the `<!-- skill:<name> -->` block into each
  *detected* harness file (`detect()` table), and the idempotent `SessionStart`
  aggregator hook (the marker + command are owned here, one source of truth).
- A **capability manifest** beyond the prose blurb: a small machine-readable
  `capabilities.json` (`{ tool, version, commands: [{ name, summary, args }],
  hook_points: [...], provider_board?: ... }`) so the ecosystem can *enumerate*
  what every installed tool can do — the CTO's "advertising tools and their
  capabilities" beyond a SessionStart text dump. The `.blurbs/` aggregator becomes
  the human view; `capabilities.json` is the structured view a meta-tool (or an
  agent) can query.

This is the **single most duplicated module (4 copies, one admitted hand-port)**,
so it is the recommended **first extraction** — see §10. It needs a Python `lib/py/
advertise` and a TS `lib/ts/advertise` over **one** `lib/contracts/skill-install`
spec (the markers, paths, hook command, harness table) so the two can never drift —
which is exactly the bug they have today (kept in sync by a comment).

---

## 7. Other genuinely-shared pieces

- **Structured logging — already done.** `lib/agenttools_log` (Python, stdlib-only
  JSONL). The **TS twin is missing**: tg's hook runner writes its own `AuditLine`
  jsonl (`runner.ts:AuditLine`) and the audit format matches agenttools_log's
  one-object-per-line shape. A `lib/ts/agenttools_log` twin would let tg emit the
  same record shape. Low priority, but it is the proof that the twin pattern is the
  right call (one shared *record shape*, two tiny implementations).
- **Audit jsonl** — implemented 3× (review registry `_audit`, tg hook runner
  `AuditLine`, and implicitly any future hook host). Folds into the **trust
  kernel** (§1/§5): "trusted dynamic load/run" always emits the same audit row, so
  the audit writer ships *with* the trust kernel, keyed off `lib/<lang>/logging`.
- **Retry / backoff** — the CTO listed it, but **no real implementation exists yet**
  (no `retry.ts`; no `backoff`/`max_retries` in review backends or tg transport).
  This is a *prospective* shared module, not an extraction of duplication. Provider
  HTTP calls (`_openai_compatible_request`) and tg's Telegram transport both *want*
  retry-with-backoff (429 / transient 5xx). Ship `lib/<lang>/retry` (a small
  `retry(fn, {tries, backoff, retry_on})`) as a *new* primitive once the provider
  layer lands — don't pretend it's de-duplication.
- **Process exec** — review's `reviewlib/process.py` (`_run`, `_run_streamed`,
  sidecar logs) is a substantial subprocess wrapper. It is review-specific (live
  panel logs) but the `_run(argv, cwd, timeout) -> proc` core is the kind of thing
  every CLI re-rolls. Candidate for `lib/py/proc` later; not urgent.
- **Render / PDF** — tg's `features/render/*`, `md-pdf`, `code-pdf` are
  tg-specific delivery; `spec-send`/review also make PDFs. Genuinely shared *only*
  if a second tool needs Telegram-shaped output. Not in the first wave.

---

## 8. Proposed `lib/` layout

```
agent-tools/lib/
  contracts/                      # language-agnostic, the source of truth
    agents-hooks-v1.md + .schema.json   # the hook contract (today only in a README + vendored in tg)
    skill-install.md + .schema.json     # markers, paths, hook command, harness table (today copied 4×)
    providers.schema.json               # provider/backend manifest shape
    board.default.yaml                  # the default provider board (data, not code)
    modules-manifest.schema.json        # the self-describing module manifest (review-visual/v1 generalized)
    capabilities.schema.json            # the tool capability manifest

  py/                             # Python implementations (review, rig, draw, task)
    agenttools_log/               # EXISTS today — the proof module
    config/                       # cascade + env precedence + ~/.config paths
    providers/                    # board, failover, transports, oc routing, key cascade
    registry/                     # self-describing module loader + TRUST KERNEL
    hooks/                        # agents-hooks/v1 dispatcher (Python twin of tg's runner)
    advertise/                    # install-skill + capability manifest
    retry/                        # (new) retry/backoff
    proc/                         # (later) subprocess wrapper

  ts/                             # TypeScript/Bun implementations (tg, future TS tools)
    agenttools_log/               # (new) twin of the record shape
    config/                       # twin
    providers/                    # twin (so tg-classify + a TS research tool route identically)
    hooks/                        # EXISTS in spirit — tg's runner.ts is the reference; move it here
    advertise/                    # twin of install.ts
    retry/                        # (new)
```

Tools depend on `lib/<lang>/<module>` and on `lib/contracts/*` (the latter as data,
not code). git-hooks/ci/mcp under `agent-tools/` stay where they are; rig's catalog
already knows that layout (`riglib/catalog.py`).

---

## 9. The hard question: Python vs TypeScript — RECOMMENDATION

The tools split across runtimes (review/rig/draw/task = Python; tg + future TS =
TS). A single importable `lib/` cannot serve both. The three options from the brief:

- **(a) Twin implementations** of every module in both languages.
- **(b) Language-agnostic CONTRACTS in `lib/` + per-language thin implementations.**
- **(c) Python-only lib; TS tools follow the same architecture by convention.**

**Recommendation: (b) — contracts-first, with selective twinning.** Concretely a
**hybrid of (a) and (b)**:

> The *valuable, must-not-drift* part of every shared seam is its **contract**
> (the wire/file format, the markers, the exit-code semantics, the board data,
> the manifest schema). Put those in `lib/contracts/` as language-agnostic specs +
> JSON-Schema + data files — **one source of truth**. Then ship **thin per-language
> implementations** (`lib/py/*`, `lib/ts/*`) that *read the same contract*. Twin
> only the modules a TS tool actually needs; everything else stays Python-only by
> convention until a second TS consumer appears (YAGNI).

Why not the pure options:

- **Pure (a) twin-everything** is wasteful — `proc`, review's streamed logs, rig's
  catalog have zero TS consumers. Twinning them is dead code in TS.
- **Pure (c) convention-only** is exactly the failure mode we already have:
  install-skill is "convention-only" today, and the result is a **hand-ported TS
  copy kept in sync by a code comment** (`install.ts`: "Mirror of the Python
  implementation … so all three tools register identically"). Convention without a
  shared artifact drifts silently — the markers/hook-command/harness-table MUST be
  one file both read, or the next edit to one breaks cross-tool registration.

**Which modules to TWIN (real `lib/py` + `lib/ts`) vs convention-only:**

| Module | Twin? | Why |
| --- | --- | --- |
| `advertise` (install-skill) | **TWIN** over a shared contract | 4 copies incl. an admitted TS hand-port; the markers/hook-command MUST be one shared artifact or registration drifts. |
| `hooks` (`agents-hooks/v1`) | **TWIN** over a shared contract | Host can be Python OR TS (tg is TS; agent-tools hooks are Python). Contract already vendored in tg; promote it to `lib/contracts`. |
| `providers` | **TWIN** over shared board data | task-cli (Py) AND tg-classify (TS) both want classification. Board/aliases/endpoints are *data*; the failover algo is small. |
| `config` | **TWIN** over a shared schema | rig/review (Py) + tg (TS) all hand-roll cascade+precedence; tiny code, the *rule* is the contract. |
| `registry` + trust kernel | **TWIN** (shared with hooks) | review (Py) + tg hook gate (TS) are the same algorithm in two langs. |
| `agenttools_log` | TS twin **when** a TS tool needs the record shape (tg does, for audit) | Record shape is the contract; impl is ~80 lines. |
| `retry` | TWIN (new, small) | Both runtimes make flaky network calls. |
| `proc`, render/PDF, rig `catalog` | **Python/convention-only** | No TS consumer today. Don't twin on spec. |

**Where the shared model-provider layer lives (the explicit ask).** The provider
*board, aliases, per-provider endpoint + key-var table, and default failover pool*
live in **`lib/contracts/board.default.yaml` + `providers.schema.json`** — data both
runtimes read. The *algorithm* (resolve → availability → startup/mid-run failover →
oc routing) is twinned: `lib/py/providers` (task-cli, research-cli, review import it)
and `lib/ts/providers` (tg's classify hook imports it). Because the board is one
data file, a Python classifier and a TS classify-hook **route to the same provider
with the same fallback order by construction** — no drift. review-cli keeps its
review-specific *role lenses* (`REVIEW_ROLES`) and panel orchestration in `reviewlib`;
it imports the generic board/failover from `lib/py/providers`.

---

## 10. Phased extraction plan

Principle: **highest leverage, lowest churn first.** Pick the seam with the most
copies and the smallest, most-stable surface so the first extraction proves the
twin-over-contract model with minimal risk.

### Phase 1 — `advertise` (install-skill) + the `skill-install` contract. **FIRST.**
- **Why first:** 4 copies (review/rig/draw Python + tg TS), the TS one an admitted
  hand-port; smallest surface (~150 lines); zero runtime behaviour change (it only
  writes dotfiles); and it immediately kills the worst drift risk (markers/hook
  command kept in sync by a comment). It also *establishes the `lib/contracts` +
  `lib/py` + `lib/ts` skeleton* the rest of the plan reuses.
- **Deliverable:** `lib/contracts/skill-install.{md,schema}` (markers, paths, hook
  command, harness table, blurb-aggregator), `lib/py/advertise`, `lib/ts/advertise`.
- **Tool change:** review/rig/draw `install.py` → call `lib.py.advertise.install_skill({name, skill_md, blurb})`; tg `install.ts` → `lib.ts.advertise`. Each tool keeps only its OWN `SKILL_MD`/`blurb` string.

### Phase 2 — `agents-hooks/v1` contract → `lib/contracts` + `lib/py/hooks` + `lib/ts/hooks`.
- **Why second:** the contract already exists (agent-tools README + tg's vendored
  `types.ts`); promoting it to `lib/contracts` and twinning the runner removes the
  "vendored" drift. tg's `runner.ts` is the reference TS impl (pure, injected I/O) —
  move it in; write the Python twin (new but small).
- **Bundles the trust kernel** (shared with Phase 4's `registry`).
- **Tool change:** tg imports `lib/ts/hooks`; agent-tools reference hooks gain a
  Python host via `lib/py/hooks`; a new tool gets pre-action hooks for free.

### Phase 3 — `lib/providers` (the big asset). 
- **Why third:** highest *value* but highest *surface* (backends.py is 1083 lines)
  and it carries review's most safety-critical logic (failover, oc privilege guard).
  Extract after the skeleton + contract pattern are proven. Generalize `ReviewResult`
  → `ProviderResult`, lift the board/failover/transports, move the board to
  `lib/contracts/board.default.yaml`. Twin to TS only the resolve+board+failover+oc
  path that tg-classify needs.
- **Unblocks** task-cli's classifier and research-cli without a 2nd copy.

### Phase 4 — `lib/config` + `lib/registry` (trust kernel) + `lib/retry`.
- `config`: fold rig's cascade + tg's env precedence + the `~/.config` path
  conventions behind one contract. 4 hand-rolls collapse.
- `registry`: generalize review's visual-module loader; share the trust kernel with
  `lib/hooks` (Phase 2).
- `retry`: new primitive, shipped with the provider layer for 429/5xx.

### Phase 5 (opportunistic) — `lib/ts/agenttools_log` twin, `capabilities.json`
manifest in `advertise`, `lib/py/proc`. As consumers appear.

---

## 11. What each tool imports / changes (summary)

- **review-cli (Py):** `install.py` → `lib.py.advertise`; `config.py`/`backends.py`/
  `panel.py` → `lib.py.config` + `lib.py.providers` (keeps `REVIEW_ROLES` + panel
  orchestration local, imports board/failover/transports/oc); `features/visual/
  registry.py` → `lib.py.registry` trust kernel; gains `lib.py.retry` on HTTP calls.
- **rig-cli (Py):** `riglib/install.py` → `lib.py.advertise`; `riglib/config.py` →
  `lib.py.config` cascade. `catalog.py`/`plan.py`/`actions` stay rig-specific.
- **draw-cli (Py):** its install-skill copy → `lib.py.advertise`. Optionally
  `lib.py.providers` if it ever picks a model dynamically.
- **tg-cli (TS):** `features/install-skill/install.ts` → `lib.ts.advertise`;
  `features/hooks/*` → `lib.ts.hooks` (its runner becomes the reference impl moved
  into lib); `features/config/env.ts` → `lib.ts.config`; the planned classify hook →
  `lib.ts.providers`; audit lines → `lib.ts.agenttools_log` record shape.
- **task-cli / research-cli (Py, future):** import `lib.py.providers` (board +
  failover, `pool=1` cheap-first) + `lib.py.config` + `lib.py.advertise` from day
  one — they spin up on the uniform base, which is the CTO's whole point.

---

## 12. Honest caveats

- **Twinning has a cost.** Every twinned module is two implementations to keep
  behaviourally identical. The contract-in-`lib/contracts` mitigates *format* drift
  but not *behaviour* drift; each twin pair needs a shared conformance test vector
  (e.g. one `skill-install` fixture both `advertise` impls must reproduce byte-for-
  byte). Budget for those fixtures — they are the thing that makes (b) safer than
  the convention-only status quo.
- **Don't over-generalize "adapter."** §2: three unrelated seams wear the name;
  only the provider/backend adapter is shared. Resist a `lib/adapters` grab-bag.
- **Retry is net-new, not de-duplication.** §7: ship it as a primitive, don't sell
  it as collapsing existing copies (there are none).
- **The provider layer is safety-critical.** Its `oc:` privilege-escalation guard
  (`_opencode_runs_in_repo`) and the failover "usable verdict" semantics
  (`result_is_usable` / `FailoverOutcome.degraded`) must move verbatim — these are
  load-bearing security/correctness invariants, not refactor fodder. Extract with a
  full test port, last (Phase 3), not first.
