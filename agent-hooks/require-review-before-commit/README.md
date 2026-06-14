# require-review-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 20

On a `git commit`, checks that an AI code review ran recently for the current change. If
none did, it **blocks** with a reminder to review the uncommitted diff first.

## How it knows a review ran

It looks for a **marker file** that your review tool touches on a successful run, and
checks the marker is fresh (within a window, default 1h). Wire it up like:

```bash
review --uncommitted && touch "${REVIEW_MARKER:-$HOME/.cache/agent-tools/last-review}"
# or
codex exec review --uncommitted && touch "$REVIEW_MARKER"
```

Configure via env:

- `REVIEW_MARKER` — marker file path (default `~/.cache/agent-tools/last-review`)
- `REVIEW_FRESH_WINDOW_S` — how recent the marker must be, in seconds (default `3600`)

## Why an agent-hook

The review has to happen *before* the commit, mid-session, while the diff is still
uncommitted — a git-hook fires too late to prompt "go review first" usefully, and can't
know whether a *session* review ran. This enforces the `ai-review-before-commit` /
`pre-commit-gate` skills.

## Fail-open, on purpose

`on_error: "open"`. This is process discipline, not a security boundary — a crash in the
check must never make committing impossible. (Contrast `block-no-verify` and
`block-secrets-write`, which are fail-closed.) It blocks only when it can *confirm* no
recent review ran.

## Test

```bash
chmod +x require_review.py
rm -f ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"block ...  exit=10

mkdir -p ~/.cache/agent-tools && touch ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"allow"  exit=0
```
