# Research #256: Thin AST/LSP Capability Layer Across Agent Harnesses

Date: 2026-07-12
Task: agent-tools #256
Worktree: main working tree, report-only artifact
Input: agent-tools #257 Haft research report
Scope: read-only research, plus this draft report artifact

## Summary

Build a thin capability layer, not a new code-intelligence product.

The layer should answer one question for an agent or rig: "For this harness, repo, and
language, what code-intelligence capability is available, and which provider should handle
it?" It should normalize a small set of operations such as symbol overview, find definition,
find references, diagnostics, and symbol-scoped edits. It should prefer harness-native
capabilities when present, then use provisioned tools only for missing capability slots.

Serena and Sverklo are reference material and optional fallback providers. The historical
evidence still does not justify adopting either as the primary system. Haft is a separate
governance/reasoning system: useful for decisions, evidence, specs, and file-to-decision
lookup, but outside the AST/LSP lane.

## Evidence Inspected

Task records:

- `task read 256`: acceptance criteria for the unified thin AST/LSP layer.
- `task read 257`: completed Haft research criteria and proof pointer.
- `task find "Serena Sverklo Haft AST LSP evolve symbol code intelligence"` returned no
  extra task records beyond the known task path.

Repo docs and code:

- `docs/specs/2026-07-11-haft-research.md`
- `docs/specs/2026-06-15-thirdparty-tool-triage.md`
- `docs/specs/2026-06-15-harness-layer-redesign.md`
- `mcp/README.md`
- `skills/universal/serena/SKILL.md`
- `skills/universal/semantic-code-search/SKILL.md`
- `.serena/project.yml`
- `rig-cli/riglib/evolve/symbols.py`
- `rig-cli/riglib/project_tools.py`
- `rig-cli/docs/config-schema.md`
- `rig-cli/riglib/harness_skills.py`
- `lib/cc_hook_bridge/README.md`
- `lib/codex_hook_bridge/README.md`
- `lib/opencode_hook_bridge/README.md`

Local harness/config evidence:

- `~/.codex/config.toml`: Codex has MCP registrations for `playwright`,
  `haft`, `node_repl`, and `pencil`.
- Current Codex `tool_search` for `LSP serena sverklo haft code symbol references`
  exposed no Serena, Sverklo, Haft, or LSP namespace in this session.
- `~/.claude.json`: global Claude MCP registrations include `serena`,
  `context7`, and `sverklo`.
- `~/.claude/settings.json`: permission allowlist includes selected Serena
  tools and `mcp__ide__getDiagnostics`.
- `~/.config/opencode/opencode.json`: global opencode MCP config exposes
  `pencil`; no Serena/Sverklo/Haft registration found there.
- `~/.config/rig/config.yaml`: still contains unsupported `harness.kinds`,
  matching the #257 finding that rig status can be blocked by config/schema skew.
- `~/.sverklo/registry.json` and `sverklo list`: six registered repos as of
  this pass, with most recent indexes 11-12 days old except `hyper-canvas-draft`
  at 56 days old.
- `~/.sverklo/*/tool-stats.json`: Sverklo product-side calls are mostly
  `sverklo_status`; only one recorded `sverklo_search` in the inspected stats.

Historical JSONL evidence:

- Parsed 11,297 files under `~/.claude/projects/**/*.jsonl` and
  `~/.codex/sessions/**/*.jsonl` for actual `tool_use` / `function_call`
  records, not prompt mentions.
- Counts found:
  - Haft: `haft_spec_section` 9, `haft_query` 6, `haft_note` 2,
    `haft_commission` 1, plus Claude MCP `mcp__haft__haft_query` 5 and
    `mcp__haft__haft_note` 4.
  - Serena: one each of `find_symbol`, `find_referencing_symbols`,
    `get_symbols_overview`, `check_onboarding_performed`, `list_memories`, and
    `write_memory`.
  - Sverklo: `sverklo_list_repos` 2, `sverklo_status` 1, `sverklo_review_diff` 1.
- Representative files with actual code-intelligence calls:
  - `~/.claude/projects/-Users-*-work-hyperide/<session>/subagents/agent-*.jsonl`
  - `~/.claude/projects/-Users-*-xp-hypercalendarbot/<session>/subagents/agent-*.jsonl`
  - `~/.claude/projects/-Users-*-work-hyper-canvas-draft/<session>/subagents/workflows/<workflow>/agent-*.jsonl`

## Current Tools Compared

| Tool / surface | What it is good for | Current role in this proposal |
| --- | --- | --- |
| `rg` / targeted file reads | Literal strings, config keys, error messages, fast local exploration | Keep as the default fast path for literal search. |
| `rig evolve` symbols | Stdlib Python/JS/TS symbol extraction with provider-shaped `rig.evolve.symbol.v1` output | Use as the seed schema and no-server fallback for symbol overviews. |
| Harness-native LSP / IDE tools | Diagnostics, definitions, references, and editor-backed code intelligence when exposed by the harness | Prefer first when present; detect at runtime. |
| Serena | LSP-backed symbol overview, find symbol, references, diagnostics, and symbol-scoped edits | Reference/fallback provider only; do not make primary. |
| Sverklo | Indexed multi-repo code search, concepts, refs, review-diff, impact/deps, memories | Reference/fallback provider only; candidate for cross-repo search if fresh and indexed. |
| Haft | Decisions, ProblemCards, FPF/spec lookup, evidence, drift, WorkCommission lifecycle | Out of AST/LSP boundary; integrate only through code-change governance metadata. |
| MCP policy | Documents why stateful code search can justify MCP, while local CLIs should stay CLI + skill | Keep: the thin layer should not wrap shell-callable stateless tools in MCP by default. |

## Why Serena And Sverklo Stay Reference-Only

Serena has the right LSP primitives, but adoption remains tiny. The inspected JSONL shows
only three actual symbol/navigation calls across all parsed Claude/Codex history:
`find_symbol`, `find_referencing_symbols`, and `get_symbols_overview`, each once. The local
skill already says Serena is an opt-in exception to grep and explicitly rejects Serena's
memory store as durable agent memory.

Sverklo has a broader and more interesting surface, especially cross-repo concept/search
operations, but current evidence does not support making it the default. It is reachable in
Claude configs, visible in local Sverklo state, and now has six registered repos, but the
historical transcript calls are still only `list_repos`, `status`, and one `review_diff`.
Product-side stats inspected under `~/.sverklo/*/tool-stats.json` are mostly
status probes, with only one `sverklo_search`.

The lesson to reuse from both is the capability taxonomy and provider behavior:
symbol overview, definition, references, diagnostics, semantic search, and optional
symbol-scoped edits. The lesson not to reuse is "install a large overlapping tool and hope
agents pick it."

## Haft Boundary

Use the #257 report as authoritative for Haft's capability surface.

Haft belongs to the decision/spec/governance lane:

- Durable micro-decisions and rationale.
- Problem framing and solution comparison.
- Decision records, evidence, baselines, and measurements.
- Drift/staleness checks.
- WorkCommission lifecycle.
- `haft_query related` and affected-file metadata that connect code paths to decisions.

Haft does not own:

- Symbol extraction.
- Go to definition.
- Find references.
- Rename.
- Diagnostics.
- Call graphs.
- AST-aware edits.

The thin AST/LSP layer can optionally ask Haft "what decisions mention this file?" after a
provider returns paths or symbols, but it should never route code navigation through Haft.

## Proposed Thin Layer

### API Shape

Keep the API small and provider-neutral. Start with read operations and add edits only after
the detection/provisioning path is reliable.

Suggested commands or library functions:

```text
codeintel capabilities --json [--repo <path>]
codeintel symbols <file> --json
codeintel definition --file <file> --line <line> --col <col> --json
codeintel references --file <file> --line <line> --col <col> --json
codeintel diagnostics <file> --json
codeintel search --literal <text> --json [--scope repo|workspace|registered]
codeintel search --semantic <query> --json [--scope repo|workspace|registered]
codeintel edit replace-symbol-body --file <file> --line <line> --col <col> --body-file <path> --provider <optional-provider>
```

Suggested JSON concepts:

- `capability`: `symbols`, `definition`, `references`, `diagnostics`, `search`,
  `rename`, `replace_symbol_body`, `cross_repo_search`, `file_decisions`.
- `provider`: `native`, `rig-evolve`, `serena`, `sverklo`, `haft`, `rg`.
- `confidence`: `native`, `indexed`, `syntactic`, `literal`, `stale`, `unavailable`.
- `freshness`: timestamp, index age, or `live`.
- `reason`: why the provider was selected or skipped.
- `result_schema`: use or extend `rig.evolve.symbol.v1` for symbol nodes.

Unsupported results should be structured, not stderr prose:

```json
{
  "status": "unavailable",
  "capability": "references",
  "provider": null,
  "reason": "no live provider exposes references for this language",
  "fallback": "rg"
}
```

The `--provider` flag on edit commands is an override/debug hint, not the normal contract.
The default path should still let `codeintel` pick the safest available provider and report
the chosen provider in JSON output.

Provider selection order:

1. If the harness exposes native LSP/IDE capability for the requested operation, use it.
2. For symbol overview on supported files, use `rig evolve` stdlib extraction as the local
   fallback.
3. For LSP-only operations such as references/rename, use Serena only when the repo is
   configured, the server is reachable, and the language is supported.
4. For cross-repo semantic search, use Sverklo only when the target repo set is indexed and
   fresh enough for the configured threshold. The inspected local indexes are mostly
   11-12 days old, so an initial seven-day threshold would classify the current Sverklo
   state as stale. Start with a configurable default such as 14 days unless a project
   proves faster indexing.
5. For literal search, use `rg` directly.
6. For file-to-decision or spec governance metadata, call Haft after code providers return
   paths, not before.

### Capability Detection

Detection should be an executable probe, not config inference:

- `native`: inspect the current harness tool list and run a harmless capability probe where
  possible, for example diagnostics on a known file or an empty symbol request. Current
  examples include Claude-side `mcp__ide__getDiagnostics` allowance and deferred `LSP`
  attachments in historical Claude transcripts.
- `codex`: use `tool_search`/active tool namespaces first. In this session, Codex config
  contains Haft but active deferred tools did not expose Haft/Serena/Sverklo, so config alone
  is insufficient.
- `claude-code`: inspect global/project MCP servers and deferred tool exposure. Treat
  deferred tools as present-but-not-loaded until a ToolSearch or equivalent confirms them.
- `opencode`: inspect `opencode.json` MCP and plugin config. Current global config only
  shows Pencil for MCP; code intelligence should be reported missing unless a project adds it.
- `gemini`, `pi`, `commandcode`: use the instruction-file/harness registry and report
  unsupported/missing providers until real tool discovery is proven.
- `serena`: require `.serena/project.yml`, supported language, server reachability, and
  either current tool exposure or a configured launch path.
- `sverklo`: require `sverklo list` registration, fresh index age under a configurable
  threshold, and the relevant command/tool availability.
- `haft`: require `haft serve`/tool exposure for governance metadata only.

### Provisioning By Harness

Rig should provision missing capabilities by harness family:

- Claude Code: prefer native IDE/LSP tools if available; otherwise register Serena/Sverklo
  only as optional code-intelligence MCPs. Keep permission allowlist narrow to read ops by
  default; symbol edits need an explicit edit capability gate.
- Codex: do not assume `[mcp_servers.*]` means tools are exposed in the session. Add a
  `rig status`/`rig doctor` probe that checks actual tool visibility. If Codex gains native
  LSP, prefer it; otherwise expose a small codeintel CLI/skill and optional MCP providers.
- opencode: because skills are natively discovered but MCP config is currently sparse, start
  with CLI + skill plus optional project MCP registration. Avoid CC-only assumptions.
- Gemini/pi/commandcode: provision instructions and a CLI path first. Do not claim LSP/MCP
  support until a runtime probe exists.
- All harnesses: `rig status` should report a capability matrix such as
  `symbols: native|rig-evolve|missing`, `references: native|serena|missing`,
  `cross_repo_search: sverklo fresh|stale|missing`, `file_decisions: haft|missing`.

## Validation Plan

1. Unit-test provider selection with fake capability probes:
   - native present beats Serena/Sverklo.
   - stale Sverklo is skipped for cross-repo search.
   - `rig evolve` fallback handles Python/JS/TS symbol overview.
   - Haft is never selected for AST/LSP operations.
2. Add fixture tests using real local sample files for `rig.evolve.symbol.v1` compatibility.
3. Add dry-run integration probes per harness config:
   - Claude Code config with Serena/Sverklo present.
   - Codex config with Haft configured but not active in tool exposure.
   - opencode config with only Pencil present.
4. Add stats classification tests so `rig stats` groups `mcp__serena__*`,
   `mcp__sverklo__*`, `mcp__haft__*`, and direct `haft_*` calls under stable families.
5. Run an end-to-end smoke in one Python repo and one TS repo:
   - `capabilities`
   - `symbols`
   - `definition` or explicit unsupported result
   - `references` or explicit unsupported result
   - `diagnostics` or explicit unsupported result
6. Verify the report path for unsupported capabilities is useful: it should say what is
   missing, where rig would provision it, and which fallback is being used.

## Next Implementation Tickets

1. Add `codeintel` capability schema and provider-selection library around
   `rig.evolve.symbol.v1`.
   Acceptance: unit tests cover native-first, rig-evolve fallback, Serena fallback,
   Sverklo freshness, Haft exclusion, and literal `rg` routing.

2. Add harness capability probes to rig-cli.
   Acceptance: `rig status` or `rig doctor` reports code-intelligence capabilities per
   harness without relying on config alone, including the current Codex "configured but not
   exposed" case.

3. Extend `project_tools` from "provision tool carriers" to "report capability readiness."
   Acceptance: Serena and Sverklo readiness include server/CLI availability, repo config,
   language/index freshness, and actual harness exposure where applicable.

4. Add stats taxonomy for code-intelligence families.
   Acceptance: `rig stats` classifies Serena, Sverklo, Haft, native IDE/LSP diagnostics,
   and `rig evolve` codeintel calls separately from generic `other`.

5. Create a universal `code-intelligence-routing` skill or update
   `semantic-code-search` and `serena`.
   Acceptance: agents get a short decision table: `rg` for literal, native codeintel first,
   rig-evolve for symbol overview fallback, Serena for LSP-only symbol refs/edits, Sverklo
   for fresh cross-repo search, Haft only for decision/spec context. Existing Serena routing
   instructions must not remain as a competing doctrine.

6. Add one guarded symbol-edit operation after read operations are proven.
   Acceptance: `replace-symbol-body` is available only when a provider proves
   reference-aware edits and the harness write gates can inspect the intended file changes.
   `rename` stays deferred until the same provider-safety contract is proven.

## Open Questions

- Whether Claude Code's native/deferred `LSP` tool is stable enough to target directly, or
  whether the thin layer should treat it as an opportunistic native provider only after a
  captured fixture.
- Whether the codeintel layer should live in rig-cli because `rig evolve` already owns the
  symbol schema, or in agent-tools as portable content with rig-cli as the first consumer.
  The smaller first step is a rig-cli library with agent-tools docs/skill routing.
- Whether 14 days is the right default freshness threshold for Sverklo cross-repo search.
  The local evidence would mark most current indexes stale at seven days, so seven days
  should be a stricter project override rather than the default.
