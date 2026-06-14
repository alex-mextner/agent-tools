# MCP slots

This directory documents two MCP (Model Context Protocol) server slots that make the
"AI review before commit" and "code search over grep" rules *executable from any agent*,
rather than just advisory text.

These are **documentation of where the servers plug in**, not the servers themselves —
the review server is a wrapper you point at your own multi-model review CLI, and the
code-search servers are third-party. Nothing proprietary is vendored here.

## `review/` — multi-model review as an MCP

A read-only multi-model code-review tool, exposed as an MCP server, turns these rules into
a callable capability:

- `ai-review-before-commit` — review the uncommitted diff before committing.
- `gan-critic-loop` — a separate critic (a different model) judging the work.
- `visual-proof-cycle` — `--visual` verification of a rendered screenshot via a vision
  model.

See `review/README.md` for the tool surface (review / quorum / brainstorm / visual) and
how it connects to the `require-review-before-commit` agent-hook and the optional photo /
notification hook.

## Code-search MCP (serena / sverklo / language-server style)

The `semantic-code-search` skill recommends an index-backed code-search server over
`grep` + whole-file reads. Those servers (serena, sverklo, and similar) are third-party —
register them per their own docs. The slot here is just the reminder that an agent's
default for "find this symbol / its references" should be such a server when one is
available, falling back to grep only for literal-string searches or unindexed repos.

Typical registration is an entry in your agent harness's MCP config pointing at the
server's launch command; consult the specific server's documentation for the exact shape.
