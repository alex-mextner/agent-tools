---
name: secret-handling
description: Use when your backend stores or handles credentials — API tokens, OAuth tokens, session secrets, private keys. Encrypt them at rest, never log them in plaintext, and never commit them to the repo.
---

# Secret handling

A credential is the keys to something — an account, an API, a user's data. Treat it
accordingly at every point it passes through your system.

## Rules

- **Encrypt secrets at rest.** Tokens and credentials stored in a database should be
  encrypted, not stored as plaintext columns. A DB dump or backup leak then exposes
  ciphertext, not live credentials.
- **Never log a secret in plaintext.** No `log.info({ token })`, no printing an
  authorization header, no dumping a config object that contains a key. Redact before
  logging. Logs get shipped to third-party aggregators and read by many eyes.
- **Never commit a secret.** Keys live in environment / a secret manager, referenced
  through config (see `config-loadconfig`), never as literals in source. A `.env` with
  real values is git-ignored. (A `no-secrets` pre-commit scan and a `block-secrets-write`
  agent-hook back this up — see `git-hooks/` and `agent-hooks/`.)
- **Don't return secrets in API responses.** A serializer that blindly spreads a DB row
  into the response will leak the token column. Allow-list the fields you return.
- **Rotate-ability.** Assume any secret can leak; design so rotating it is a config
  change, not a code change.

## Why

Secrets leak through the boring paths — a log line, a committed `.env`, an over-broad
JSON serializer, a plaintext DB column in a leaked backup — far more often than through
a sophisticated attack. Each rule closes one of those paths. Encryption at rest and
no-plaintext-logging in particular turn a data leak from "attacker now has live
credentials" into "attacker has useless ciphertext". Pairs with
`fail-closed-security`.
