---
name: node-builtin-prefix
description: Use when importing a Node.js built-in module in server-side code. Use the `node:` protocol prefix so the import is unambiguously the built-in, not a same-named package, and resolvers/bundlers treat it correctly.
---

# Import Node built-ins with the `node:` prefix

A bare `import { readFile } from "fs"` is ambiguous: a package named `fs` on npm (or a
local module) could shadow the built-in, and some bundlers/runtimes treat bare specifiers
differently from explicit built-ins. The `node:` protocol prefix removes the ambiguity.

## Rule

Prefix every Node built-in import with `node:`:

```ts
// GOOD — unambiguously the built-in.
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";

// Avoid — bare specifier; shadowable, and some tools treat it as an external dep.
import { readFile } from "fs/promises";
```

This applies to *server-side* code that targets Node (or a Node-compatible runtime like
Bun, which honors the prefix). It does not apply to browser code (no Node built-ins
there) or to third-party packages (those stay bare).

## Why

The prefix makes intent explicit to humans and tools alike: bundlers know to externalize
it rather than try to bundle a "module" named `crypto`; resolvers can't be fooled by a
malicious or accidental same-named package; and a reader sees instantly that it's a
platform built-in, not a dependency. It's a small, mechanical habit that removes a class
of resolution surprises. Many linters can autofix bare built-in imports to the prefixed
form.
