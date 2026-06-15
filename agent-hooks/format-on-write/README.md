# format-on-write

**Point:** `post-write` · **Fail policy:** `open` · **Priority:** 60 (runs late) · **Never blocks**

After the agent writes or edits a file, this hook runs the **project's configured
formatter** on *just that one file*, in place. Formatting becomes a **harness** concern —
the agent's file writes come out formatted automatically — instead of a per-project /
per-extension manual step or a commit-time cleanup.

It is the generalization of "an `oxfmt --write` step in `lefthook.yml`" (or a
`package.json` `lint:fix`): the same idea, but lifted out of any one repo and made to run
on every write the agent does, in any project, for any of the supported languages.

## Why `post-write` (not `pre-write`)

A formatter rewrites a file **in place**, so the file has to exist first. `pre-write` only
sees the *proposed* bytes (and is for **blocking** a bad write before it lands). Formatting
is the opposite shape: let the write land, then clean it up. That needs a point that fires
*after* the file is on disk — `post-write`, added to the contract for exactly this class of
"react to a completed write" hooks. See `../README.md` → "Hook points used here".

## Detection table

Resolves the formatter by **(repo config, file extension)**, walking up from the written
file to the **git** repo root (the `.git` dir). A repo-**local** tool
(`node_modules/.bin/<tool>`) is always preferred over a global one — same resolution
`lefthook.yml` / npm scripts use. The first candidate that is configured/available wins;
the rest are skipped. The table is **fixed** (not project-configurable); an unsupported
tool falls through to a no-op.

> Repo-local lookup is anchored on the `.git` root, so it only fires **inside a git
> worktree**. Outside one (no `.git` up the tree), only globally-installed formatters
> (`gofmt`, `rustfmt`, a global `oxfmt`/`prettier`) can apply — repo-local bins are invisible.

"Configured/available" means, per tool: a repo-local bin, OR (for oxfmt/prettier) the tool
named in `package.json` **and** present on PATH, OR (biome) a `biome.json(c)` at root +
on PATH, OR (ruff/black/gofmt/rustfmt) simply present on PATH.

| Extension | Candidates (in order) | How it's detected | Invocation |
| --- | --- | --- | --- |
| `.js .jsx .mjs .cjs .ts .tsx .mts .cts .json .jsonc .css .md .mdx .html .yaml .yml` | **oxfmt** → **prettier** → **biome** | oxfmt: `node_modules/.bin/oxfmt`, else `oxfmt` in `package.json` + on PATH. prettier: same. biome: `node_modules/.bin/biome`, else `biome.json(c)` at root + on PATH. | `oxfmt --write --no-error-on-unmatched-pattern <f>` / `prettier --write <f>` / `biome format --write <f>` |
| `.py .pyi` | **ruff** → **black** | local/global `ruff`; else local/global `black` | `ruff format <f>` / `black -q <f>` |
| `.go` | **gofmt** | `gofmt` on PATH (ships with Go) | `gofmt -w <f>` |
| `.rs` | **rustfmt** | `rustfmt` on PATH (ships with Rust) | `rustfmt <f>` |

If the extension isn't in the table, or no candidate is configured/available → **no-op,
allow**. The hook never reformats the whole tree (it always passes the single written
file), and never installs a formatter.

## Escape hatch

```bash
NO_FORMAT_HOOK=1   # the hook no-ops immediately (allow), before touching the file
```

Use it when you want to inspect the agent's raw output, are mid-bisect, or a formatter is
misbehaving.

## Never blocks (fail-open, by design)

`on_error: "open"`, and the script has **no `block` path at all**. Every failure mode — no
formatter, tool missing on PATH, formatter non-zero (e.g. a half-written file with a syntax
error), timeout, unparsable event — resolves to `allow`. Formatting is a convenience; it
must never wedge the agent. The `pre-commit` git-hook format step remains the backstop that
*gates* on formatting.

Two timeouts bound a run: the script kills a formatter after **8s** (`RUN_TIMEOUT_S`), and
the descriptor's host-level `timeout_ms` (**10s**) is the hard ceiling — the inner bound is
deliberately shorter so a wedged formatter is caught by the script (→ allow) before the
host's `on_error` fires.

## Install

```bash
chmod +x format_on_write.py
# Set the descriptor's "cmd" to this file's absolute path, then drop the descriptor into
# your harness's post-write hook directory (map "post-write" to the harness's
# "file was written/edited" event — e.g. a PostToolUse matcher on the Write/Edit tools).
```

`rig apply` does all three (and rewrites the `cmd` placeholder to the absolute path).

## Test

Automated (hermetic — no network, no real formatters; a fake local bin proves the
local-bin preference and end-to-end run):

```bash
uv run --with pytest python -m pytest tests/test_format_on_write.py -q   # from repo root
```

Manual smoke (needs a real formatter on PATH/repo for case 1):

```bash
chmod +x format_on_write.py

# 1) formats a deliberately mis-formatted file (needs a formatter for that ext on PATH/repo)
tmp="$(mktemp -d)/messy.py"; printf 'x=1\ndef  f( a,b ):\n  return  a+b\n' > "$tmp"
echo "{\"args\":{\"path\":\"$tmp\"}}" | ./format_on_write.py 2>&1; echo "exit=$?"
cat "$tmp"   # → reformatted (if ruff/black is installed)

# 2) no-op when no formatter maps to the extension
tmp2="$(mktemp -d)/notes.txt"; echo "hi" > "$tmp2"
echo "{\"args\":{\"path\":\"$tmp2\"}}" | ./format_on_write.py 2>&1; echo "exit=$?"
# → "no formatter mapping for '.txt' — allow (no-op)"  decision allow  exit=0

# 3) escape hatch
NO_FORMAT_HOOK=1 sh -c "echo '{\"args\":{\"path\":\"$tmp\"}}' | ./format_on_write.py" 2>&1
# → "NO_FORMAT_HOOK=1 — skipping (allow)"
```

Every case prints `{"hook_api":"agents-hooks/v1","decision":"allow"}` and exits `0`.
