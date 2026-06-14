# Merge gate: mandatory screenshots on UI PRs

"A user-visible change with no screenshot isn't proven." This slot enforces it: if a PR
**touches UI paths** (configurable) it must carry an **embedded image** in its body or
comments, or the check fails. Two enforcement points — **PR-open** (the workflow) and
**merge-time** (call the script from your ship preflight, [`../ship/`](../ship/)) — so a UI
change can't lose its proof between review and merge.

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `require-screenshots.sh` | **Any CI** + ship preflight | Decides UI-touching via a path glob, then fails unless an image is embedded in the PR. |
| `workflow.yml` | **GitHub Actions** | Runs the script on PR open/edit/push as the `screenshots` check. |

## Quick start

```bash
cp ci/screenshots/workflow.yml .github/workflows/screenshots.yml
# The workflow runs `bash ci/screenshots/require-screenshots.sh`, so the script must be
# present at that path — vendor the ci/ dir, or copy the script and adjust the run: path:
cp ci/screenshots/require-screenshots.sh .github/scripts/require-screenshots.sh
# Optional hard block: Settings -> Branches -> required checks -> add "screenshots".

# Standalone / merge preflight:
PR_NUMBER=123 sh ci/screenshots/require-screenshots.sh
```

## Knobs — parameterize the rule

- `UI_PATH_REGEX` — ERE matched against changed file paths. If any changed file matches,
  a screenshot is required. Default covers common front-end dirs (`components/`, `pages/`,
  `views/`, `ui/`, `app/`) and `*.tsx/*.jsx/*.vue/*.svelte`. **Set it for your layout.**
- `REQUIRE_ALWAYS=1` — require a screenshot on **every** PR, ignore the path glob.
- `IMAGE_REGEX` — what counts as an embedded image (Markdown `![](http…)`, `<img>`,
  GitHub user-attachment URLs). Widen/narrow as needed.
- `ALLOW_NO_SHOT='<reason>'` — escape hatch for a genuine non-visual change on a UI path
  (e.g. a CSS-variable rename): logs the reason and passes. Keep it honest.

## Why two enforcement points

The PR-open check tells the author *now*. But a screenshot can be removed during review, or
a UI change can be added late. A **merge-time** re-check (in the ship script) is the
backstop. The CTO's rule — "image attachments at PR-creation AND at merge-time" — is exactly
these two points; this slot supplies the same script for both.

## gh can't upload images — note

`gh` cannot attach an image file to a PR from the CLI. This gate only *verifies* an image is
present; producing/uploading it is the author's job (drag into the PR on the web, or use a
tool that posts to an image host and embeds the URL — the [`../ship/`](../ship/) script has
an optional uploader hook). The companion `visual-proof-cycle` skill covers capturing and
actually *looking at* the screenshot before claiming it works.

## When to use

Any repo with a front-end where "looks right" matters. Pairs with the
[`../pr-checklist/`](../pr-checklist/) template (which has a Screenshots section) and the
`visual-proof-cycle` skill.
