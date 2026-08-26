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

## The proof contract (how it knows a screenshot was looked at)

**Preferred: a bound attestation from `dev shot`.**

```bash
dev shot http://localhost:4173/ --out shot.png --viewport 1440x900
```

That command captures (viewport before open, scroll sweep so reveal-on-scroll content is
actually visible), measures the PNG, and writes a JSON record ONLY if the capture is
admissible — rejecting a blank page, a large blank band, a wrong-width capture, or a
viewport-only shot of a tall page. The record carries:

- `capture_sha256` — the bytes that were measured;
- `staged_sha256` — sha256 of `git diff --cached` at capture time.

The gate recomputes the staged hash and re-hashes the capture file, so a record **cannot** be
reused for a different change, cannot survive further staging, and cannot outlive the image
it describes.

**Legacy: any fresh file in the marker dir.** Any file in `VISUAL_PROOF_DIR` (default
`~/.cache/agent-tools/visual-proof`) with an mtime within `VISUAL_PROOF_WINDOW_S` (default
`3600`s) still satisfies the gate, so repositories that have not adopted `dev shot` keep
working. Set `VISUAL_PROOF_REQUIRE_BINDING=1` to refuse it and demand a bound attestation.

Why the legacy form is weak: a bare `touch` satisfies it, so it proves nothing about whether
a screenshot exists or whether it was blank. Both failures happened in practice — one capture
attested on in good faith was 42% a single blank band, because reveal-on-scroll sections were
photographed while still at `opacity: 0`.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so
> each call picks up the current env — this is fine, not a footgun.

## Block, not warn (doctrine)

Doctrine says "block a commit that changes UI with no attached screenshot", so this is a
straight block on the first occurrence — but it is **satisfiable** (touch the marker after you
review the capture).

## Not subagent-exempt

A subagent committing UI work must also have looked at the result, so this gate does **not**
exempt subagents (unlike the orchestration gates 1–3).

## No self-service bypass — request a Telegram approval instead

There is **no** env var or inline sentinel an agent can set on its own command to skip this
gate (a self-grant is security theater). If you have a genuine reason to commit UI work with
no fresh screenshot marker, **ASK the human**, or request a **one-time Telegram approval**:

```bash
RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="deleting a dead component, nothing to render" git commit -m x
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_VISUAL_PROOF_GATE=…` assignment out of the command string the event carries
(a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd` prefix, so
the value never reaches its `os.environ`). Exporting the var into the harness environment works
too and takes precedence over an inline value.

The request routes to the human over Telegram (`tg-ctl ask`) and the commit is allowed **only**
on their approval. It is **deny-by-default**: a blank value or a bare `1`/`true` (no real
justification) is rejected without even sending the message, and any nonzero/timeout/error
verdict denies. This replaces the old `ALLOW_NO_VISUAL_PROOF` / `# visual-proof-ok:` self-service
hatch (removed — an agent setting its own bypass is not a control).

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
