# worktree-only-writes

**Point:** `pre-write` · **Fail policy:** `open` · **Priority:** 40 (runs before the other pre-write gates)

Enforces the **worktree-only workflow**: on a PR-workflow repo the **default branch**
(main/master) is for **merge / pull / read-only** operations only. All file-authoring work
belongs in a **separate git worktree on a feature branch**. This gate DENIES an
`Edit/Write/MultiEdit/NotebookEdit` while the checkout is sitting on the default branch and
tells the agent to create/enter a worktree first. (Alex tg#5742.)

It is the mid-session complement to the pre-push **`protect-main`** git-hook
(agent-tools#157): that hook blocks a direct *push* to main; this one blocks the *authoring*
that would precede it — redirecting the agent to a worktree **before** it writes, not after
it tries to push. Read-only tools and `git merge` / `git pull` are unaffected (they are not
Edit/Write tool calls, so this point never sees them).

## Default-branch detection (not hardcoded, never machine-derived)

1. `git symbolic-ref --short refs/remotes/origin/HEAD` → `origin/main` → `main` (authoritative)
2. repo-local `git config --local --get init.defaultBranch` (NOT the global setting)
3. a local branch that exists — `main` preferred, then `master`
4. fallback `main`

A `master`-only repo is judged against `master`. Each step is repo-local, so enforcement never
swings with the developer's global git config.

## Per-repo opt-in (this must NOT break repos that work on main)

Enforcement is **opt-in**, first match wins:

1. `RIG_WORKTREE_ONLY=1` (force on) / `=0` (force off) — session/CI override, and how the
   tests drive it.
2. the repo's committed `rig.yaml` → `agent_hooks.worktree_only: true` — the rig-provisioned
   signal.
3. **default OFF** — a repo with no signal is never blocked.

So `hyperide` and the agent-ecosystem repos set `worktree_only: true` in their `rig.yaml`;
`3d-cli` (which legitimately works directly on main) leaves it absent and is exempt
automatically. See rig-cli `docs/config-schema.md` for the knob.

## Escape hatch (the rare, deliberate main edit)

Mirrors `block-raw-pr-merge` / the build guards:

```bash
RIG_ALLOW_MAIN_EDIT=1 RIG_ALLOW_MAIN_EDIT_REASON="hotfix, worktree overkill" <edit on main>
```

`RIG_ALLOW_MAIN_EDIT_REASON` is optional but logged when present.

## Why an agent-hook

Advice in `AGENTS.md` cannot stop an auto-mode agent from editing on main. A git-hook fires
only at commit/push — too late, and the pre-push guard already covers that stage. The only
place to redirect *authoring* to a worktree is **before the write**, which is what a
`pre-write` agent-hook does.

## Fail-open

`on_error: "open"`. If the branch can't be determined (not a git checkout, git missing, a
timeout) the write is **allowed** — a workflow nudge must never wedge the agent.

## Test

```bash
chmod +x worktree_only_writes.py
# on the default branch, enrolled → BLOCK
RIG_WORKTREE_ONLY=1 sh -c 'echo "{\"cwd\":\"$PWD\",\"args\":{\"file_path\":\"a.ts\"}}" | ./worktree_only_writes.py'; echo "exit=$?"
# not enrolled (default) → allow, exit=0
echo '{"cwd":"/tmp","args":{"file_path":"a.ts"}}' | ./worktree_only_writes.py; echo "exit=$?"
```
