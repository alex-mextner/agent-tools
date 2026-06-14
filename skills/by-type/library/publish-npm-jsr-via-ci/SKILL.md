---
name: publish-npm-jsr-via-ci
description: Use when publishing a library to a registry. Publish from CI on a tag/release, not from a developer's laptop, and use `import type` with verbatim module syntax so type-only imports are erased correctly from the build.
---

# Publish from CI, and use `import type`

Two library-publishing practices that prevent a class of "works on my machine, broken in
the package" problems.

## Publish from CI, not a laptop

A `npm publish` run by hand from a developer's machine ships whatever happens to be in
that working tree, with that person's local toolchain, against credentials on their disk.
It's unreproducible and a credential-leak risk. Instead, publish from a **CI workflow**
triggered by a tag or release:

- the published artifact is built from a clean checkout of a known commit;
- the registry token lives in CI secrets, not on laptops;
- the same workflow can publish to multiple registries (npm **and** JSR) consistently;
- the release is auditable — there's a CI run for every published version.

## Use `import type` with verbatim module syntax

For type-only imports, write `import type` (and enable `verbatimModuleSyntax`):

```ts
// GOOD — explicit type-only import; erased from the JS output, no runtime import emitted.
import type { Options } from "./options";
import { run } from "./run";              // value import, kept

// Risky — a type used only as a type but imported as a value can leave a dangling
// runtime import (or break tree-shaking / dual ESM-CJS builds).
import { Options, run } from "./run";
```

`verbatimModuleSyntax` makes the compiler emit exactly what you wrote — value imports stay,
`import type` is erased — which is what produces correct ESM/CJS output and avoids
accidental runtime dependencies on type-only modules.

## Why

CI publishing makes releases reproducible, credential-safe, and multi-registry-consistent
— none of which a manual laptop publish gives you. `import type` + verbatim syntax makes
the emitted module graph match your intent, which matters most exactly in a published
library that others import in environments you don't control. Pairs with
`library/version-changelog-assert` (CI can gate the publish on the version/changelog
check).
