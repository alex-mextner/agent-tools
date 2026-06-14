---
name: dependency-version-ranges
description: Use when adding or updating a dependency in a manifest. Use a caret range so patch and minor fixes flow in, rather than pinning an exact version — unless you have a documented reason to pin.
---

# Use caret ranges, don't pin exact versions

Pinning every dependency to an exact version (`1.4.2`) freezes you out of patch and
minor releases — including security fixes — until someone manually bumps each one.
Across a real dependency tree that manual bumping never happens consistently, so
pinned projects quietly accumulate known-vulnerable, known-buggy versions.

## Rule

- Default to a **caret range** so compatible patch and minor updates flow in:

  ```jsonc
  "dependencies": {
    "some-lib": "^1.4.2"     // accepts 1.4.x and 1.x, not 2.0
  }
  ```

- The **lockfile** (committed) is what pins the exact resolved versions for
  reproducible installs — that's its job, not the manifest's.
- **Pin exact** only with a documented reason: a dependency that breaks semver, a
  known-bad later version, a deliberately frozen toolchain. Leave a comment saying
  why, so the next person doesn't "helpfully" loosen it.

## Why

The manifest expresses *what you're compatible with*; the lockfile expresses *what
you resolved to*. Ranges in the manifest let `update` pull in fixes within your
compatibility envelope; the lockfile keeps installs deterministic. Pinning the
manifest collapses both jobs into one and gets you neither — you lose the fixes and
gain nothing the lockfile didn't already give you.
