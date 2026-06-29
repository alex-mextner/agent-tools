# Test gate — run the repo's own test suite on every PR

The one CI check every project actually needs: **does the code still pass its tests?**
Every other gate in this catalog is governance (titles, secrets, leftovers, review
threads); this is the gate that runs the repo's `pytest` suite and goes red when a change
breaks it. It is the green/red signal that `gh ship` (and the branch ruleset) gate merges
on, so a repo with no test workflow has nothing for them to wait on.

Dependency-free by design: it installs [uv](https://github.com/astral-sh/uv) and runs
`uv run --with pytest pytest tests/`, so it needs **no secrets** and no committed lockfile
or `pyproject` — it works on a plain stdlib + uv repo (like this one) and on a fully
specified project alike. Actions are pinned to commit SHAs.

## Quick start

```bash
cp ci/tests/workflow.yml .github/workflows/tests.yml
# Runs `uv run --with pytest pytest tests/` from the checkout root. No script companion,
# no secrets. Adjust the test path / extra deps via the env knobs below.
```

Or let `rig` provision it: set in `rig.yaml`

```yaml
ci:
  enabled: true
  items:
    tests: { tier: block }
```

then `rig apply`.

## Enforcement — a REQUIRED check, or it does not block the merge button

`tier: block` makes the workflow **go red** on a failing suite — but a red workflow by itself
does **not** block the merge button. To actually ENFORCE it, the `tests` context must be a
**REQUIRED status check** under **server-side branch protection**. The client-side
[`gh ship`](../ship/) gate *does* refuse to merge over red `tests`, but a GitHub-UI merge or a
raw `gh pr merge` bypasses ship entirely — exactly how hyper-saas #543 merged through red CI.
rig-cli#5 provisions the required-check from the `github:` block in `rig.yaml` (it lifts every
`tier: block` gate, including `tests`, into `required_status_checks`). See
**[Client-side vs. server-side enforcement](../../README.md#client-side-vs-server-side-enforcement-the-543-gap)**
in the repo README.

## Knobs

| env | default | purpose |
| --- | ------- | ------- |
| `TEST_PATHS` | `tests` | Space-separated paths/files passed to pytest. (Paths with spaces are unsupported — they word-split into separate args.) |
| `TEST_EXTRA_DEPS` | (empty) | Extra `--with <pkg>` deps for the test run (e.g. `pyyaml requests`). |
| `PYTEST_SPEC` | `pytest>=8,<9` | Pinned pytest requirement, so a new pytest major can't flip a green PR red on its own. Bump deliberately. |

The interpreter is pinned (`python-version: "3.12"` on `setup-uv`) so the baseline doesn't
drift as `ubuntu-latest` rolls forward.

Triggered by `pull_request` (runs the PR's own code in the restricted, secretless context —
never `pull_request_target`) plus `push` to the default branch so the default branch keeps a
green baseline.
