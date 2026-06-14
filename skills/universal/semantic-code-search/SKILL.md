---
name: semantic-code-search
description: Use when locating a known symbol, its definition, or its references in a sizeable codebase. Prefer a semantic/index-backed code-search tool over grep-plus-read-whole-file — it's faster, cheaper in context, and finds references grep misses.
---

# Prefer semantic code search over grep + full-file read

When you already know *what* you're looking for — a function, a type, its callers —
grepping a string and then reading whole files to find the definition is slow and
burns a lot of context on irrelevant lines. An index-backed code-search tool
(language-server / symbol-index style: serena, sverklo, context7, or your editor's
"go to definition" / "find references") answers the same question directly.

## When to reach for which

- **Find a definition** — symbol search ("go to definition") lands on it directly,
  no file-reading scan.
- **Find all callers** — references search finds them including ones a naive grep
  misses (re-exports, aliased imports, method calls on a typed value).
- **Understand a symbol's shape** — a symbol overview returns the signature and
  members without dumping the whole file into context.
- **Plain grep is still right** when you want a *literal string* (an error message, a
  magic constant, a config key) or you're in a language/repo with no index.

## Why

Two reasons. **Cost**: reading a 2000-line file to find one function wastes context
you'll want later; a symbol lookup returns just the relevant span. **Correctness**: a
references query understands the language's binding rules, so it finds the call that
grep's string match overlooks and skips the comment that grep's string match falsely
hits.

This pairs with `dead-code-investigation`: before concluding something is unused, a
references query (which sees more than grep) is part of the evidence.
