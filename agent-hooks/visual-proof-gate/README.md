# visual-proof-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 46

Fires on a `git commit`. If the staged change touches **user-visible** files, it **blocks**
unless a fresh "I looked at a screenshot" marker exists. Enforces `visual-proof-cycle`:
capture the rendered result, read the capture back, verify it — **then** commit. A "done"
claim on a UI change with no screenshot you actually looked at is the failure this stops.

## What counts as user-visible (staged file inspection)

The gate shells out to `git -C <cwd> diff --cached --name-only` and flags a staged file if:

- its extension matches `\.(tsx|jsx|vue|svelte|css|scss|less|html|svg|png|jpg|jpeg|gif|webp)$`, **or**
- its path is under `components/`, `ui/`, `pages/`, `app/`, `views/`, `public/`, `assets/`.

If **no** user-visible file is staged → **allow** (nothing to prove). If git can't be queried
(not a repo, timeout, error), the lister returns nothing and the gate **fails open**.

## The marker contract (how it knows a screenshot was looked at)

The `visual-proof-cycle` skill / a screenshot-capture step **touches a file** after the agent
VIEWS the capture:

```
~/.cache/agent-tools/visual-proof/<key>     # mtime = "looked at it" time
```

Any fresh file in that dir (within `VISUAL_PROOF_WINDOW_S`, default `3600`s) satisfies the
gate. Configure the dir with `VISUAL_PROOF_DIR`. This is the honest, satisfiable action —
look at the screenshot, the capture step records that you did.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so
> each call picks up the current env — this is fine, not a footgun.

## Block, not warn (doctrine)

Doctrine says "block a commit that changes UI with no attached screenshot", so this is a
straight block on the first occurrence — but it is **satisfiable** (touch the marker after you
review the capture) and **escapable** (below).

## Not subagent-exempt

A subagent committing UI work must also have looked at the result, so this gate does **not**
exempt subagents (unlike the orchestration gates 1–3).

## Escape hatch (controllable, not a hard wall)

```bash
ALLOW_NO_VISUAL_PROOF=1 ALLOW_NO_VISUAL_PROOF_REASON="CSS var rename, no visual change"   # session
git commit -m x   # visual-proof-ok: deleting a dead component, nothing to render
```

A reasonless `ALLOW_NO_VISUAL_PROOF=1` is ignored and the commit stays blocked.

## Fail-open, on purpose

`on_error: "open"`. Process discipline, not a security boundary. The `git diff --cached`
subprocess is timeout-bounded (5s) and fails open — a broken stat must never wedge committing.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x visual_proof_gate.py
# in a repo with a staged .tsx and no fresh marker:
echo '{"cwd":"'"$PWD"'","args":{"command":"git commit -m x"}}' | ./visual_proof_gate.py
rc=$?; echo "exit=$rc"   # → exit=10 (block)

# touch a marker after looking at a screenshot → allow
mkdir -p ~/.cache/agent-tools/visual-proof && touch ~/.cache/agent-tools/visual-proof/looked
echo '{"cwd":"'"$PWD"'","args":{"command":"git commit -m x"}}' | ./visual_proof_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (marker fresh → allow)
```
