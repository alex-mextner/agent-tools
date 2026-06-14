---
name: figma-design-tokens
description: Use when syncing design values (colors, spacing, typography) from Figma into code. Pull them via the Figma REST API with a personal access token kept in an ignored env file, and make the fetch a reproducible, committed command.
---

# Figma → design tokens via the REST API

Hand-copying hex values and spacings out of Figma into code drifts the instant a
designer changes anything, and there's no record of where the numbers came from. Pull
them programmatically and make the pull reproducible.

## Pattern

- Use the **Figma REST API** with a **personal access token** to read the file's styles /
  variables, and transform them into your design-token format (CSS variables, a tokens
  file, your theme config).
- **Keep the token in an ignored env file**, never in source or the committed script:

  ```bash
  # .env  (git-ignored — see backend/secret-handling)
  FIGMA_TOKEN=...
  FIGMA_FILE_KEY=...
  ```

- Make the fetch a **committed, reproducible command** (an npm script / shell script) that
  reads the token from env, so anyone can re-sync after a design change:

  ```bash
  # scripts/sync-tokens.sh — reproducible; token comes from env, not the script.
  curl -fsSL -H "X-Figma-Token: $FIGMA_TOKEN" \
    "https://api.figma.com/v1/files/$FIGMA_FILE_KEY/variables/local" \
    | node scripts/transform-tokens.mjs > src/theme/tokens.generated.ts
  ```

## Why

The API + token approach makes the design→code link explicit and repeatable: re-running
one command re-syncs every value, with no manual copying and no drift. Keeping the token
in an ignored env file (not the script) means the reproducible command is safe to commit
and share. Pairs with `dark-theme-semantic-tokens` — the generated tokens are exactly the
semantic roles your components reference, and with `backend/secret-handling` for the token.
