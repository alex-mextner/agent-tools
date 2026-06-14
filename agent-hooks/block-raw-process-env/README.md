# block-raw-process-env

**Point:** `pre-write` · **Fail policy:** `open` · **Priority:** 50

Flags a write/edit that introduces a **raw environment read** —
`process.env.X`, `import.meta.env.X`, `os.environ[...]`, `os.getenv(...)` — inside
configured **feature directories**, where config should instead come through a single
validated loader (the `config-loadconfig` skill). The config-loader file itself is exempt:
it is the one place allowed to touch the environment.

- **default (advisory):** warns and allows.
- **strict** (`BLOCK_PROCESS_ENV_STRICT=1`): blocks.

## Configuration (adapts to any layout)

- `PROCESS_ENV_FEATURE_DIRS` — colon-separated path fragments to watch
  (default `src/bot:src/services:src/commands:src/features`)
- `PROCESS_ENV_CONFIG_PATHS` — colon-separated fragments that *are* the config loader and
  are exempt (default `config:/env/:loadConfig`)

## Why an agent-hook

It catches the raw env access at the moment it's *written*, so the fix ("route it through
config") happens before the pattern spreads. A git-hook grep could catch it at commit time
too — but the pre-write nudge is earlier and points at the specific edit. Either way it
enforces the same `config-loadconfig` skill; use whichever fits your workflow (or both).

## Fail-open

`on_error: "open"`, advisory by default — config discipline is a code-quality rule, not a
safety boundary, so it should never block file writes outright unless you opt into strict.

## Test

```bash
chmod +x block_raw_process_env.py
echo '{"args":{"path":"src/services/pay.ts","content":"const k = process.env.STRIPE_KEY"}}' | ./block_raw_process_env.py 2>&1; echo "exit=$?"
# default: warns, allow, exit=0  (strict: block, exit=10)

echo '{"args":{"path":"src/config/env.ts","content":"const k = process.env.STRIPE_KEY"}}' | ./block_raw_process_env.py; echo "exit=$?"
# → allow, exit=0  (this IS the config loader — exempt)
```
