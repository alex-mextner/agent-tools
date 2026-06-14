---
name: lazy-heavy-imports
description: Use when building a CLI whose commands pull in heavy dependencies. Keep module-top imports light (stdlib only), and lazy-import heavy deps inside the command's run function so help, version, and offline commands stay fast.
---

# Lazy-import heavy dependencies inside run()

If every command module imports its heavy dependencies at the top of the file, then
`cli --help` — which runs no command — still pays the cost of importing *all* of them.
A CLI that takes two seconds to print its help, or fails offline because one command's
optional dep can't load, is paying for work it isn't doing.

## Rule

- **Module top: stdlib / import-light only.** What's needed to *register* the command —
  its name, help text, argument shape — and nothing heavy.
- **Heavy deps: lazy-import inside `run()`**, so they load only when that command actually
  executes:

  ```python
  # command module — top level is cheap; importing this file is fast.
  def run(args):
      import numpy as np          # heavy dep imported only when this command runs
      import trimesh              # ...not when the CLI merely lists/help's
      ...
  ```

  ```ts
  // TS equivalent — dynamic import inside the handler.
  export async function run(args: string[]) {
    const { default: heavyLib } = await import("heavy-lib");
    ...
  }
  ```

A test that asserts the module tree imports with stdlib only (no heavy deps at import
time) keeps this from regressing — see `git-hooks/` for packaging it as a gate.

## Why

Three payoffs. **Fast feedback**: `--help`, `--version`, and tab-completion stay instant
because they import nothing heavy. **Offline resilience**: a command whose optional/native
dep is missing doesn't break the *whole* CLI — only that command fails, and only when run.
**Lower footprint**: short-lived invocations don't load megabytes they never use. The cost
is one `import` line moved inside a function. Pairs with `self-registering-commands`
(each discovered module stays cheap to load) and `idempotent-bootstrap`.
