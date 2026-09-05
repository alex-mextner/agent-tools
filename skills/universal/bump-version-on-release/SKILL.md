---
name: bump-version-on-release
description: Use when releasing/shipping a code change in a versioned tool — a CLI, package, or service that declares a version. Bump the declared version (pyproject.toml / package.json) every release, and make `--version` read that declared version dynamically instead of a hardcoded literal that silently goes stale.
---

# Bump the version on every release — and read it dynamically

A tool's version is a freshness signal: it lets a user (and you) tell which build they're
running and whether a fix actually landed. That signal only works if two things hold on
every release:

1. the **declared** version is bumped, and
2. `--version` reads that **same declared** value at runtime.

Break either and the version lies. The canonical failure: `rig --version` printed a
hardcoded `0.1.0` for the entire life of the tool — the literal was baked into the code and
the version was never bumped across many releases. So `--version` told every user `0.1.0`
no matter which build they had: a useless, permanently-stale string, not a freshness signal.
Worse, a hardcoded literal can drift from the declared `pyproject.toml`/`package.json`
version, so the two disagree and neither is trustworthy.

## Rule

- **Bump the declared version on every release** of a code change. The declared version is
  the single source of truth:
  - Python: `[project] version = "X.Y.Z"` in `pyproject.toml`.
  - Node: `"version": "X.Y.Z"` in `package.json`.
- **Semver the bump to the change:**
  - **patch** (`0.4.1` → `0.4.2`) — a bugfix, no API change.
  - **minor** (`0.4.1` → `0.5.0`) — a backward-compatible feature.
  - **major** (`0.4.1` → `1.0.0`) — a breaking change.
- **`--version` MUST read the declared version dynamically — never a hardcoded literal.**
  Resolve it from package metadata at runtime so it can't drift:
  - Python: `importlib.metadata.version("your-dist")` (installed dist), or read the
    `[project] version` from `pyproject.toml` for a source checkout.
  - Node: read `version` from `package.json` (e.g. `require('./package.json').version`).
- **Docs-only / pure-test / pure-CI / a revert** is not a release — no bump required. The
  rule fires only when **shippable source** actually changed.
- **Do NOT bump the patch version inside a PR by hand — `gh ship` does it at merge time.**
  With several PRs open in parallel, each one bumping the same line to the same next
  version makes the second to merge CONFLICTING (a rebase + re-bump chore for every PR in
  the queue). So the default is: leave the version line alone in a fix PR; when the ship
  gate would otherwise refuse for a missing bump, ship computes the next **patch** version
  and commits `chore(release): bump version X -> Y (ship auto-bump for #N)` onto the PR's
  head branch through the GitHub Contents API right before the squash-merge (updating the
  branch from base first if base's version already moved, so it never conflicts). Every
  later gate treats that commit as ship's own (CI waits on the new head; review-dwell is
  measured from your last push, not ship's; a bot nit on the bump line is auto-closed).
  Bump by hand **only** for a deliberate **minor** or **major** — ship sees a version that
  moves past the base and makes no second bump. Opt a repo out with `SHIP_AUTO_BUMP=0` in
  its committed `.ship-config` (or the env var for one run) to get the old refuse-until-bumped
  behaviour.

## Why

The version is the only thing a user can read to answer "is the fix in the build I'm
running?". If the literal is hardcoded it answers that question wrong forever; if it isn't
bumped on release it answers "same as last time" forever. Both turn the one cheap freshness
signal into noise. Reading the declared version dynamically makes drift structurally
impossible — there's one number, and bumping it is the entire release ritual.

## Generic example

```toml
# pyproject.toml — the single source of truth
[project]
name = "mytool"
version = "0.5.0"        # bump THIS every release (was 0.4.1; minor: added a subcommand)
```

```python
# mytool/cli.py — --version reads the declared version, never a literal
from importlib.metadata import version, PackageNotFoundError

def get_version() -> str:
    try:
        return version("mytool")          # dynamic: tracks pyproject's [project] version
    except PackageNotFoundError:
        return "0.0.0+unknown"            # source checkout without install metadata
    # NEVER:  return "0.1.0"   <- a literal that goes stale the moment you ship and forget
```

A release is then: ship. For a fix, `gh ship` moves the one `version` line (patch) itself at
merge time; for a minor/major you edit it in the PR and ship leaves it alone. `--version`
updates itself — there is no second place to remember, so it can't drift and can't go stale.
