# ci/ — drop-in CI tools

Reusable, vendor-neutral CI building blocks. A CI-building agent (or human) looks **here**
first for "is there already a standard way to do X in CI?" Each subdirectory is one
concern, with a GitHub Actions workflow **and** a generic shell script (for GitLab /
Jenkins / Buildkite / cron), plus a README that states the standard tool and how to extend
it.

| Slot                          | Concern         | Standard engine | Has                                  |
| ----------------------------- | --------------- | --------------- | ------------------------------------ |
| [`secret-scan/`](secret-scan/) | Secret scanning | **gitleaks**    | pinned GH Action + shell script + tiered configs |

## Conventions for slots in here

- **Name the standard engine** in the README's first lines — don't make the reader guess
  (secret scanning = gitleaks, not a hand-rolled regex).
- Ship **both** a GitHub Actions workflow (`*.yml`) and a **generic shell** entry
  (`*.sh`) so non-GitHub CI is a first-class path, not an afterthought.
- **Pin** third-party actions to a commit SHA (supply-chain hygiene), with the version in
  a trailing comment.
- Document **how to extend** (rules/config) and the **escape hatch** for false positives.
- State the **tiers** (block vs warn) explicitly — CI defaults to block; warn is opt-in.

## Relationship to the git-hooks

Many CI checks have a local-hook twin under [`../git-hooks/`](../git-hooks/): the hook gives
the committer fast feedback and the CI check is the backstop for anyone whose hook is
missing or bypassed. Secret scanning is the canonical example — same gitleaks engine, two
carriers. See [`../docs/carrier-decision-guide.md`](../docs/carrier-decision-guide.md).
