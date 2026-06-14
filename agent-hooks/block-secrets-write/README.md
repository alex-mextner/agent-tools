# block-secrets-write

**Point:** `pre-write` · **Fail policy:** `closed` · **Priority:** 5 (runs first)

Inspects the content an agent is about to write or edit into a file and **blocks** if it
contains a likely live secret:

- a private-key PEM block (`-----BEGIN ... PRIVATE KEY-----`)
- an API key / token / secret / password *assignment* with a real-looking value
- a `Bearer <token>` credential
- common provider key formats (AWS `AKIA…`, Slack `xox…`, OpenAI-style `sk-…`)

Files that legitimately hold placeholder secrets (`*.example`, `*.sample`, fixtures) are
skipped, and obvious placeholders (`YOUR_KEY_HERE`, `<token>`, `changeme`, …) don't trip it.

## Why an agent-hook (not only a git-hook)

The `no-secrets` git-hook catches secrets at *commit* time — but the secret is already on
disk by then, and a developer might `cat` it, log it, or copy it before committing. This
hook catches the secret **before it's ever written**, which is the earliest possible point.
Run *both*: this for the write, the git-hook as the commit backstop.

## Fail-closed

`on_error: "closed"`. If the scan can't run, the write is **denied** — a secret persisted
because the guard crashed is the failure this hook exists to prevent. For a public repo
this is the correct asymmetry.

## Note

This is a *heuristic pre-filter*, deliberately conservative to limit false positives — not
a replacement for a dedicated scanner. Pair it with `git-hooks/no-secrets-scan` (gitleaks)
and the `backend/secret-handling` skill.

## Test

```bash
chmod +x block_secrets_write.py
echo '{"args":{"path":"src/config.ts","content":"const apiKey = \"a1b2c3d4e5f6a7b8c9d0e1f2a3b4\""}}' | ./block_secrets_write.py; echo "exit=$?"
# → decision":"block ... exit=10   (a real-looking value assigned to a key name)

echo '{"args":{"path":"src/config.ts","content":"const apiKey = process.env.API_KEY"}}' | ./block_secrets_write.py; echo "exit=$?"
# → decision":"allow"  exit=0
```
