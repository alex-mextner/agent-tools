# permission-guard — pi coding-agent permission enforcement

A [pi coding agent](https://github.com/earendil-works/pi) extension that enforces command
permissions on the `bash` tool: it **denies** dangerous commands and **asks** before risky ones,
giving pi the same guardrail belt the other agent harnesses (claude-code, opencode) get.

## Why

pi ships **no built-in permission system** — it runs bash with the invoking user's privileges. Its
extension API, however, can intercept a tool call before it executes (`pi.on("tool_call")` →
return `{ block: true }`), which is the sanctioned way to add a permission gate. This extension uses
that hook to match each bash command against a policy and allow / ask / deny accordingly.

## How it is provisioned

`rig` installs this extension for repos whose harness is pi (`harness.kind: pi`):

- copies this folder to `~/.pi/agent/extensions/permission-guard/` (pi auto-discovers
  `~/.pi/agent/extensions/*/index.ts`; the base dir honors `PI_CODING_AGENT_DIR`);
- writes the policy it enforces to `~/.pi/agent/rig-permission-policy.json`.

Both steps are idempotent and backup-on-conflict; `rig status` reports drift.

## Policy file

`~/.pi/agent/rig-permission-policy.json` (rig owns it — the extension is generic):

```jsonc
{
  "version": 1,
  "default": "allow",
  "rules": [
    { "id": "git-force-push", "action": "deny", "command": "git",
      "argvAll": ["push"], "flagsAny": ["--force", "-f"], "reason": "…" },
    { "id": "git-reset-hard", "action": "ask", "command": "git",
      "argvAll": ["reset"], "flagsAny": ["--hard"], "reason": "…" }
  ]
}
```

A rule matches a bash command when:

- the command's **argv0 basename** equals `command` (so `/usr/bin/git` and `git` both match; an
  `env FOO=1 …` prefix is skipped);
- every entry in `argvAll` matches some argv token **by basename** (so a subcommand naming a
  binary — e.g. sudo's `"rm"` — still matches `sudo /bin/rm`/`sudo ./rm`, not only a bare `rm`
  token; this applies to EVERY remaining argv token, not just the one actually executed, so an
  unrelated path argument that happens to share a basename with a guarded entry — e.g.
  `sudo cp /bin/rm /tmp/rm.bak` against an `argvAll: ["rm"]` rule — also matches); and
- if `flagsAny` is set, at least one of those **exact** tokens appears anywhere in the argv (this
  one stays literal-token, unlike `argvAll`).

Exact-token, flag-**anywhere** matching is deliberate: it catches `git commit -m "x" --no-verify`
(which prefix globs miss) yet never confuses `--force` with `--force-with-lease`. Compound commands
(`a && b`, `a | b`, `a; b`) are split and evaluated per clause; the **strongest** decision wins
(deny > ask > allow). Both `argvAll`/`flagsAny` (camelCase) and `argv_all`/`flags_any` (snake_case)
keys are accepted. `argvAll`'s basename-broadening is the safe over-match direction only because
every `PolicyRule.action` is `"ask"` or `"deny"` — there is no per-rule `"allow"` action (allow
comes only from `Policy.default`), so this broadening can never widen what gets allowed.

## Fail-closed

If the policy file is **missing** the extension uses its baked-in baseline, silently (a clean
machine / not-yet-provisioned repo is expected and not an error). If the file is **present** but
unreadable (permission/ownership drift, a directory in its place) or unparseable/invalid, it also
falls back to the baseline but **logs a warning** — the dangerous denies fire even with zero or
corrupt config, and a policy that exists-but-is-broken is surfaced rather than silently disabled.
In a **non-interactive** run (`ctx.hasUI` false) an `ask` decision is **blocked** (nothing can
prompt), while `deny` always blocks.

## Baseline enforced

| Command | Decision |
|---|---|
| `gh pr merge …` | deny (merges go through `gh ship`) |
| `git push … --force` / `-f` | deny (`--force-with-lease` is allowed) |
| `git … --no-verify` | deny (bypasses hooks) |
| `sudo rm …` | deny |
| `screencapture …` | deny (use Playwright/CDP) |
| `pkill` / `killall` | ask |
| `git reset --hard …` | ask |

The baseline mirrors the argv-level intent of the rig agent-hooks and the claude-code deny/ask
rules. It is kept in **SYNC** with rig's `riglib/permissions.py` (`PI_DENY_RULES` / `PI_ASK_RULES`),
which is what rig serializes into the policy file.

## Scope / limitations

This is a **lightweight argv matcher for the commands an agent actually emits**, not a hardened
sandbox against a determined adversary. It resolves argv0 through the common wrapper forms
(`VAR=val …`, `env VAR=val …`, `env -i/-C/-u/--unset/--chdir/-iu … cmd`) and evaluates each clause
of a `&&`/`||`/`|`/`;` chain, but by design it does **not** defeat deliberate obfuscation:
`sh -c '…'`, command substitution `$(…)`, a quoted `env -S "git push --force"`, combined
short-flag bundling (`git push -fq`), or a **leading** redirection before the command
(`>/dev/null git push --force`, where the redirect target becomes the apparent argv0). Those stay
out of scope here exactly as they do for the argv-level agent-hooks and the claude-code prefix
rules — which remain the deep enforcement layer underneath. For a hard boundary, containerize pi.

## Test

```bash
npm test   # tsx --test — pure matcher tests (policy.test.ts), handler wiring tests (index.test.ts),
           # and policy-loader tests (loader.test.ts)
```
