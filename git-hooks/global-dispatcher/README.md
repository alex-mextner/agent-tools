# Global git-hook dispatcher

A **single, extensible** mechanism that runs a growing set of global git hooks in
**every** repo on this machine — including repos whose local hook manager
(lefthook / husky) or raw `.git/hooks` overrides `core.hooksPath` and would otherwise
shadow the global hooks entirely.

> **This supersedes `install-local-secret-scan.sh`** (the narrow, secret-scan-only
> injector left on the `ci-gate-suite` branch). That script wired ONE hook into one
> repo. This wires the **dispatcher** instead, so secret-scan *and every future global
> hook* run with no further per-repo edits. Reconcile the two at merge — keep this one.

## The model in one paragraph

There is ONE entry point, `run-global-hooks <event>`, and one directory of hooks,
`~/.config/git/global-hooks.d/<event>/`. A "global hook" is just an executable file
dropped into that directory. The dispatcher enumerates and runs them all, in sort order,
for the given event; any non-zero exit blocks. Every repo is wired to call the dispatcher
**once per event** — so adding a new global hook later is a one-file drop that
automatically takes effect everywhere, with zero per-repo changes.

```
~/.config/git/
├── run-global-hooks                 # THE DISPATCHER (one entry point)
├── global-hooks.d/                  # the single source of truth — drop hooks here
│   ├── pre-commit/
│   │   └── 10-secret-scan           # gitleaks tiered staged-diff gate (block + warn)
│   ├── commit-msg/
│   └── pre-push/
├── hooks/                           # the GLOBAL composers (core.hooksPath target)
│   ├── pre-commit                   # repo-local hook → dispatcher → review-gate
│   ├── commit-msg                   # repo-local hook → dispatcher
│   ├── pre-push                     # repo-local hook → dispatcher
│   └── review-gate                  # review-before-commit gate
├── template/hooks/                  # init.templateDir → new repos get dispatcher hooks
├── install-local-hooks.sh           # per-repo injection (ONE line, covers all hooks)
└── hooks-sweep                      # MANUAL retrofit scan (no cron, run anytime)
```

The files here in `agent-tools/git-hooks/global-dispatcher/` are the **shipped twins** of
the live `~/.config/git/` copies. Keep them in sync.

## Adding a new global hook (the whole point)

```sh
# 1. write the hook (gets event args; pre-push refs on stdin). Non-zero = block.
cat > ~/.config/git/global-hooks.d/pre-commit/20-no-todo-in-prod <<'EOF'
#!/bin/sh
git diff --cached -U0 | grep -nE '^\+.*TODO\(prod\)' && {
  echo "no-todo-in-prod: remove TODO(prod) before committing" >&2; exit 1; }
exit 0
EOF
chmod +x ~/.config/git/global-hooks.d/pre-commit/20-no-todo-in-prod
```

That's it. It now runs in EVERY wired repo. No installer, no sweep, no per-repo edit. The
number prefix controls order (`10-` before `20-`). Mirror the file into this dir in
agent-tools and commit it to keep the shipped twin current.

## Per-repo injection — `install-local-hooks.sh`

Wires ONE repo by detecting its hook manager and idempotently injecting a single
dispatcher call per event (guarded by a `global-git-hooks-dispatcher` marker — re-running
is a no-op):

| Manager  | What it injects                                                              |
| -------- | --------------------------------------------------------------------------- |
| lefthook | a `global-git-hooks-dispatcher` command under each event (MERGED, not clobbered) |
| husky    | a guarded dispatcher line appended to `.husky/<event>`                       |
| raw      | the dispatcher call inserted after the shebang in `.git/hooks/<event>` (body kept) |

```sh
install-local-hooks.sh [REPO_DIR]     # default: the repo containing $PWD
```

Repos with **no** local `core.hooksPath` override need nothing — the global composer in
`~/.config/git/hooks/` already covers them.

## Never-forget — three passive layers, NO schedule

1. **`init.templateDir`** → `git config --global init.templateDir ~/.config/git/template`.
   Every `git init` / `git clone` copies `template/hooks/<event>` (which call the
   dispatcher) into the new repo's `.git/hooks`. New raw repos are wired automatically.
2. **Manager templates** (`templates/lefthook.yml`, `templates/husky/*`) — preseeded
   with the dispatcher call. Copy one when starting a new lefthook/husky repo and it's
   wired from day one.
3. **`hooks-sweep`** — a MANUAL, run-anytime scan of `~/work` + `~/xp` that finds
   local-manager repos not yet wired and injects (idempotent) + reports. It mops up
   EXISTING repos created before wiring. **It is NOT scheduled** — no cron, no launchd.
   Run it when you remember; the passive layers above catch the rest.

```sh
hooks-sweep --dry-run                 # report what WOULD be wired; change nothing
hooks-sweep                           # wire unwired managed repos under ~/work + ~/xp
HOOKS_SWEEP_SKIP="/path/a:/path/b" hooks-sweep   # skip active-agent repos
hooks-sweep DIR1 DIR2                 # scan other roots
```

The sweep prunes worktrees, caches, `node_modules`, `cloned-projects`, and vendored deps;
it skips **linked worktrees** (their `.git` is a file) because they share the main repo's
tracked hook config — wire the main repo once and worktrees inherit it.

## Bypass / escape hatches

- `REVIEW_SKIP=1 git commit ...` or `git commit --no-verify` — bypasses ALL pre-commit
  gates (last resort).
- Secret-scan false positive: inline `gitleaks:allow` comment, or an `[allowlist]` in
  `~/.config/gitleaks/gitleaks.toml` (global) / the repo's `.gitleaks.toml`.

## Why a git hook (and where it can't reach)

Git hooks enforce mechanically checkable gates at commit/push time. A git hook literally
cannot block its own `--no-verify` bypass — rules that must fire mid-session or resist
bypass belong in `../../agent-hooks/`. See `../README.md` and `../../docs/carrier-decision-guide.md`.
