---
name: serena
description: Use when navigating, understanding, or making structured edits in a sizeable codebase, or recording durable project knowledge. Serena is an LSP-backed code-intelligence MCP — symbol search/references/overview, symbol-precise edits, diagnostics, and per-project memories — that answers "where/what/who-calls" and edits by symbol instead of by line, far cheaper in context than grep + read-whole-file.
---

# Serena — LSP-backed code intelligence + project memory

Serena is a language-server-backed MCP that turns the editor's "go to definition / find
references / rename symbol" power into agent tools, plus a small per-project memory store. It
answers structural questions and makes structural edits **by symbol**, not by reading and
rewriting whole files — so it's both cheaper in context and more correct than grep + full-file
read + string replace.

## When to reach for it

**Navigate / understand (read):**
- `get_symbols_overview` — the symbols a file defines (signatures + members) without dumping
  the whole file into context. The first thing to run on an unfamiliar file.
- `find_symbol` — jump straight to a definition by name/path (no grep-then-scan).
- `find_referencing_symbols` / `find_implementations` — every caller / every impl, including the
  ones grep misses (re-exports, aliased imports, typed-value method calls, interface impls).
- `get_diagnostics_for_file` — the language server's own errors/warnings for a file.

**Edit (structural, by symbol — safer than line/string edits):**
- `replace_symbol_body`, `insert_after_symbol` / `insert_before_symbol`, `rename_symbol`,
  `safe_delete_symbol` — edit a function/class/method as a unit. `rename_symbol` updates every
  reference; a string replace would miss some and clobber look-alikes.

**Remember (durable project knowledge):**
- `write_memory` / `read_memory` / `list_memories` — record per-project facts (architecture,
  gotchas, where-things-live) under `.serena/memories/`. This is **committed, team-shared
  knowledge** — `.serena/` belongs in the repo (its own `.serena/.gitignore` already excludes
  the volatile `cache/`). Do NOT gitignore `.serena/`.
- `onboarding` / `check_onboarding_performed` — seed a fresh checkout's project memory.

## When NOT to

- A **literal string** hunt (an error message, a magic constant, a config key) → plain grep.
- A language/repo serena can't index → grep + read.
- A tiny one-file change you're already looking at → just edit it.

## Why

**Context cost:** a symbol overview returns one signature, not 2000 lines you'll regret loading.
**Correctness:** references/rename understand the language's binding rules, so they catch the
call grep's string match overlooks and skip the comment it falsely hits. This pairs with
`semantic-code-search` (the general "prefer an index over grep" rule) and
`dead-code-investigation` (a references query is stronger evidence than grep before you call
something unused).
