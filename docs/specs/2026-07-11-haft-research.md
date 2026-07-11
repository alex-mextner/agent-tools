# Haft research: capabilities, usage, and provisioning gaps

- Status: research for task #257
- Date: 2026-07-11
- Scope: Haft first; Serena and Sverklo only as code-navigation references.

## Summary

Haft is not a code-navigation helper. It is a governance and reasoning system:
spec carriers, FPF lookup, problem framing, solution comparison, decision records,
evidence, drift/staleness checks, and WorkCommission lifecycle. It should be kept
for durable engineering decisions and spec-backed execution boundaries.

It should not replace the planned thin AST/LSP layer. The AST/LSP lane owns
symbol extraction, find references, and structured code navigation. Haft only
touches files as evidence, affected-file metadata, drift baselines, and
file-to-decision lookup.

Usage exists but is not normal workflow usage. Harness JSONL shows 27 actual
Haft calls across Claude and Codex history parsed for this task. Haft product
logs show 102 MCP calls, clustered around onboarding/bootstrap/spec maintenance,
not everyday agent work.

Provisioning is the problem. The local binary works and `haft serve` lists tools,
but rig and harness provisioning do not make Haft consistently reachable,
ready, or measured.

## Capability surface

Direct MCP handshake against `HAFT_PROJECT_ROOT=/Users/ultra/xp/hyperos haft serve`
returned 8 tools:

| Tool | Actions / function surface | Expected workflow |
| --- | --- | --- |
| `haft_note` | record micro-decisions with rationale, affected files, evidence, keywords | Use for small decisions that should be searchable later. |
| `haft_problem` | `frame`, `characterize`, `select`, `close` | Turn an unclear engineering issue into a ProblemCard before solution work. |
| `haft_solution` | `explore`, `compare`, `similar` | Generate variants, apply parity rules, identify non-dominated options. |
| `haft_decision` | `decide`, `apply`, `measure`, `evidence`, `baseline` | Persist the selected option, implementation contract, evidence, measurements, and file hash baselines. |
| `haft_refresh` | `scan`, `waive`, `reopen`, `supersede`, `deprecate`, `reconcile` | Find stale decisions/evidence and manage lifecycle without deleting history. |
| `haft_query` | `search`, `status`, `board`, `related`, `projection`, `list`, `coverage`, `fpf`, `check`, `resolve_term` | Search the artifact graph, inspect readiness, look up FPF/spec terms, render audience projections, and map files to decisions. |
| `haft_commission` | `create`, `create_from_decision`, `create_batch_from_decisions`, `create_from_plan`, `list`, `list_runnable`, `show`, `claim_for_preflight`, `requeue`, `cancel`, `record_preflight`, `start_after_preflight`, `record_run_event`, `complete_or_block` | Create bounded execution authorizations from decisions and manage runtime lifecycle. |
| `haft_spec_section` | `next_step`, `approve`, `rebaseline`, `reopen` | Drive spec onboarding, approve baselines, and manage spec drift. |

CLI help exposes the same product areas as commands: `agent`, `board`, `check`,
`commission`, `desktop`, `doctor`, `fpf`, `harness`, `init`, `models`, `run`,
`serve`, `setup`, `spec`, `sync`, and `version`.

## AST/LSP comparison

The planned thin AST/LSP layer is represented locally by `rig evolve` symbol code
and the MCP policy. `riglib/evolve/symbols.py` says its current stdlib extractor
returns provider-shaped symbol nodes so "LSP, Serena, or tree-sitter providers"
can later replace/enrich the same shape. `agent-tools/mcp/README.md` says code
search MCPs are justified for stateful index-backed `find-symbol` and
`find-references` work.

Overlap with Haft is narrow:

- `haft_query(action="related", file=...)` can connect a file to decisions.
- `haft_decision(action="baseline")` snapshots affected file hashes.
- `affected_files` fields tie decisions/evidence to paths.

Haft does not provide symbol definitions, references, rename, diagnostics,
call graphs, or AST-aware edits. Keep those in the thin AST/LSP layer. Serena
and Sverklo remain reference candidates only; their value is code navigation,
not decision governance.

## Usage evidence

Rig stats:

- Source: `/tmp/rig-stats-full.json`, generated with `rig stats show --format json`.
- Total normalized invocations: 155,429 from 2025-11-19 through 2026-07-11.
- Categories: baseline 121,698; ours 5,213; external-advertised 51; other 28,467.
- Haft calls in rig stats are Codex-only and classified as `other`: `haft_spec_section`
  9, `haft_query` 6, `haft_note` 2, `haft_commission` 1. Total 18.
- Rig stats is useful for the harness-level adoption picture, but it does not
  classify Haft as its own family and did not surface the Claude MCP calls found
  by direct JSONL parsing.

Harness JSONL parser:

- Claude source: `/Users/ultra/.claude/projects/**/*.jsonl`.
- Claude files parsed: 7,402. Actual Haft MCP calls: 9 (`mcp__haft__haft_note`
  4, `mcp__haft__haft_query` 5), across 6 session/subagent files.
- Codex source: `/Users/ultra/.codex/sessions/**/*.jsonl`.
- Codex files parsed: 3,770. Actual Haft function calls: 18 (`haft_spec_section`
  9, `haft_query` 6, `haft_note` 2, `haft_commission` 1), across 3 session files.

Haft product logs:

- Source: `/Users/ultra/.haft/logs/*.log`.
- Files with Haft MCP calls: 5.
- Total product-side calls: 102.
- By tool: `haft_spec_section` 50, `haft_query` 22, `haft_decision` 18,
  `haft_note` 6, `haft_commission` 6.
- By project: `gitapp` 51, `hyper-canvas-draft` 25, `unknown` 15,
  `hyperide` 7, `hyperos` 4.
- Interpretation: the system was used for project bootstrap/spec maintenance
  and some decision evidence, not as a routine helper invoked by most agents.

Task and prior research:

- Task source: `task read 257`.
- Prior triage source: `docs/specs/2026-06-15-thirdparty-tool-triage.md`.
- The prior triage also concluded Haft should be kept and fixed for durable
  decision records, but not confused with code-search systems.

`research-cli`:

- `research-cli` and `research` are not installed in PATH on this machine, so
  they were not useful for this investigation.

## Provisioning breakpoints

1. Current Codex config declares Haft but this session exposes no Haft tools.
   `~/.codex/config.toml` contains `[mcp_servers.haft] command = "haft" args = ["serve"]`,
   while `tool_search` for `haft` returned 0 tools. Direct MCP handshake with
   `haft serve` works, so this is a harness discovery/session exposure problem,
   not a dead binary.

2. Rig status is blocked globally before project-tool drift can be inspected.
   `rig status` in `agent-tools`, `rig-cli`, `gitapp`, and `hyperos` fails on
   `harness.kinds` in `~/.config/rig/config.yaml`: unknown key, expected only
   `auto_mode`, `enabled`, `hook_bridge`, `kind`, `mode`, `settings_path`.
   This makes provisioning drift checks unreliable until the config/schema
   mismatch is fixed.

3. `project_tools` is not actually declared in the checked repo configs inspected.
   `agent-tools` and `gitapp` `rig.yaml` files do not declare `project_tools`;
   `hyperos` has no `rig.yaml`; `rig-cli` has an untracked `.haft/` and
   untracked `.codex/config.toml`. So Haft state often exists outside active
   rig reconciliation.

4. Rig-generated `.haft` carriers are placeholders, not readiness.
   `rig-cli/.haft/specs/*` contains draft placeholder sections and an empty
   term map. `haft check` in `rig-cli` reports 3 findings: no active enabling
   sections, no active target sections, and missing term-map entries.

5. Real onboarded repos can still drift.
   `gitapp` is tracked and has real decisions/evidence, but `haft check` reports
   4 drifted spec sections. `hyperos` is clean, but it is outside rig because it
   has no `rig.yaml`.

6. Harness support is inconsistent.
   Claude has `h-reason` and `h-onboard` skill usage in `~/.claude.json`, but
   no Claude MCP Haft registration was found in the inspected config. Codex has
   user-level MCP config but current tool exposure failed. `gitapp/opencode.json`
   contains a local Haft MCP command but `enabled` is false by design. Gemini
   provisioning was not found.

7. Project-root binding is fragile.
   Rig's generated Codex section uses `HAFT_PROJECT_ROOT = "."`; hand-written
   wrappers in `gitapp/.haft/bin/serve-haft` are safer because they `cd` to the
   repo and export an absolute root. The current user-level Codex config has no
   project root env at all.

8. Stats taxonomy hides Haft.
   `rig stats` treats Codex `haft_*` calls as `other` and did not report Claude
   `mcp__haft__*` calls as a Haft family. This makes adoption reporting weaker
   unless paired with JSONL parsing.

## Recommendation

Keep Haft, but fix provisioning before pushing agents to use it.

Capability verdicts:

- `haft_note`: keep. Low-friction durable micro-decision capture.
- `haft_problem`: keep for expensive/ambiguous problems, not routine bugs.
- `haft_solution`: keep for structured alternatives and Pareto comparison.
- `haft_decision`: keep; this is Haft's main durable value.
- `haft_refresh`: keep; lifecycle/staleness is distinct from AST/LSP.
- `haft_query`: keep, especially `status`, `check`, `fpf`, `resolve_term`,
  and `related`.
- `haft_commission`: keep only for spec/decision-backed execution boundaries;
  do not make it a default task runner.
- `haft_spec_section`: keep, but treat placeholder `.haft` as not onboarded
  until this reports clean readiness.

Repair plan:

1. Fix the rig-cli/global config mismatch around `harness.kinds`, or remove the
   unsupported key from global config, so `rig status` works again.
2. Make rig's Haft provisioning explicit and verifiable per harness: Codex,
   Claude, opencode, and Gemini should be either supported with a smoke check or
   explicitly reported as unsupported.
3. Replace `HAFT_PROJECT_ROOT="."` with a stable repo-bound wrapper or absolute
   cwd/env in generated Codex registration.
4. Add a rig drift/readiness check that distinguishes "`.haft` exists" from
   "Haft is onboarded and `haft check` is clean".
5. Teach `rig stats` to classify `haft_*` and `mcp__haft__*` as a Haft family.
6. Keep the AST/LSP work separate: build/keep the thin symbol provider for
   definitions/references/renames; use Haft only to connect code changes to
   decisions, evidence, and spec governance.

No code fix was made in this pass. The investigation was reliable enough without
patching because the breakpoints are directly observable: working `haft serve`,
failing `tool_search`, blocked `rig status`, placeholder `.haft` readiness
findings, and parsed usage logs.

## Sources

- `/Users/ultra/.codex/config.toml`
- `/Users/ultra/.claude.json`
- `/Users/ultra/.claude/skills/h-reason/SKILL.md`
- `/Users/ultra/.claude/commands/h-*.md`
- `/Users/ultra/.config/rig/config.yaml`
- `/Users/ultra/xp/rig-cli/riglib/project_tools.py`
- `/Users/ultra/xp/rig-cli/tests/test_project_tools.py`
- `/Users/ultra/xp/rig-cli/docs/config-schema.md`
- `/Users/ultra/xp/rig-cli/riglib/evolve/symbols.py`
- `/Users/ultra/xp/agent-tools/mcp/README.md`
- `/Users/ultra/xp/agent-tools/docs/specs/2026-06-15-thirdparty-tool-triage.md`
- `/Users/ultra/xp/rig-cli/.haft/`
- `/Users/ultra/xp/gitapp/.haft/`
- `/Users/ultra/xp/gitapp/opencode.json`
- `/Users/ultra/xp/hyperos/.haft/`
- `/Users/ultra/.haft/logs/*.log`
- `/Users/ultra/.claude/projects/**/*.jsonl`
- `/Users/ultra/.codex/sessions/**/*.jsonl`
