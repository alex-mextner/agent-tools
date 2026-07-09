# decision-request-format

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 25 · **Advisory only (never blocks)**

Enforces the *self-check* half of the [`decision-request-discipline`](../../skills/universal/decision-request-discipline/SKILL.md)
skill at the one moment a skill can't: **send time.** When the agent sends a decision request
to the human's channel (`tg --tag decision …`), this parses the message body and, if it is
missing the structural markers the skill mandates — **Context, Options, Recommendation** — it
emits an **exit-0 ADVISORY** naming what's absent, so the agent can rewrite before the human
gets a bare "A or B?".

## Why advisory, never a block

A malformed decision request must stay **sendable.** A heuristic that wedged the send would
be worse than the malformed message — the human would rather get an imperfect escalation than
have a marker regex silently swallow it. So this hook **never returns exit 10**; it returns
`allow` with a reminder message and lets the send proceed. The deterministic value it adds
over the skill alone is purely about *timing*: it fires at send time, the exact moment the
skill's "self-check before sending" is supposed to run but routinely gets skipped under load.
A skill can only remind you to check; this checks.

## Triggered (decided from a PARSED command — never a raw substring)

Only an actual `tg` command carrying `--tag decision`:

- `tg --tag decision "<body>"` — the canonical decision request
- `--tag=decision`, and the body assembled from positional text **plus** `--title <text>`
- a `tg` later in a pipeline (`build_msg | tg --tag decision …`) — every `&&`/`;`/`|` segment
  is walked
- wrapped in leading no-op wrappers (`env TG_AI_MODEL=claude tg …`, `timeout 30 tg …`) — the
  wrapper + its own args are peeled before tokenizing

The command is split on real shell separators, each segment is **shlex-tokenized**, and the
tag + body are read **off the argv** exactly as `tg` would receive them. So none of these trip
it (the `tg` / `decision` text is data, not a command + tag):

```bash
tg "remember to use --tag decision next time"   # no actual --tag flag; it's body text
echo "send a tg --tag decision report"          # the words live inside an echo operand
tg --tag report "build green"                    # a different tag — not a decision request
git commit -m "add tg --tag decision hook"       # the flag is inside the commit MESSAGE
```

## Known boundary: a separator char inside the quoted body

The command is split on raw `&&`/`;`/`|` before each segment is shlex-tokenized, so a body
that itself contains one of those chars inside quotes — `tg --tag decision "use A | B"` —
is torn mid-quote, the segment gets an unbalanced quote, shlex raises, and that segment is
skipped. Effect: **the advisory is missed** (the send still goes through). This is a false
*negative* only — never a false positive, and never a block — which matches the hook's
advisory/fail-open posture. A full quote-aware splitter would close it but is over-engineering
for an advisory nudge; it's covered as a documented boundary in the test suite.

A few other shapes are also missed by design, all false-negative-only on an advisory hook:
`sudo tg …` and a command substitution `$(tg …)` aren't unwrapped (neither is in the wrapper
set, and substitutions aren't recursed), and a future `tg` value-flag not in the known set
would leak its value into the parsed body. None can ever *block* a send.

## Not triggered (the hook only inspects decision requests)

- any `tg` send without `--tag decision` (`--tag report`, `--tag problem`, no tag at all)
- a non-`tg` command, or a `tg` substring inside a path / another flag's value
- a decision-request body that **already has** all three markers → silent `allow`

## Marker detection is permissive on purpose

The three markers are matched by deliberately loose, case-insensitive regexes (e.g. *Options*
matches any of `option/variant/pros/cons/trade-off`). The goal is to catch a body that
mentions **none** of a dimension — the bare "A or B?" — not to grade phrasing or word choice.
A body that gestures at context, lists options, and states a recommendation passes, even if
it doesn't use those exact headings. False *positives* (advising on a fine message) cost only
a one-line stderr note since the send proceeds regardless; that asymmetry is why permissive is
correct here.

## Not subagent-exempt

The skill routes the *drafting* of a decision request to a subagent precisely so it isn't done
sloppily inline — so the self-check binds that subagent too. A subagent posting a malformed
decision request is exactly what this catches; there is no `agent_id` carve-out.

## No self-service silence — external approval only

Because it never blocks, "escaping" here just silences the advisory note — but there is **no**
`ALLOW_RAW_DECISION_REQUEST` env and **no** `# decision-request-ok:` inline sentinel any more.
An agent could set either on its own command, so those were self-grant silencers. A one-time
silence is now requested by setting
`RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<written justification>"`, which routes one Telegram
approval request to Alex (deny-by-default):

```bash
RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="terse follow-up to an already-detailed thread" \
  tg --tag decision "..."
```

Unset means the hook never contacts Telegram (the advisory prints as usual); a blank or bare
`1`/`true`/`yes`/`on` is rejected without a Telegram call; a real justification runs the trusted
`tg-ctl ask` and silences the advisory **only on exit 0**. Either way the send still goes
through — the hatch only decides whether the advisory prints.

## Fail-open, on purpose

`on_error: "open"`. A formatting reminder, not a boundary — a crash (a command shlex can't
tokenize, an unexpected event shape) must never wedge the ability to send a message. Every
path returns `allow`; the only question is whether it also prints the advisory.

## Test

Capture the hook's exit and stdout. The exit is always `0`; the signal is whether the
`allow` carries a `message`.

```bash
chmod +x decision_request_format.py

# missing all three markers → allow WITH an advisory message:
echo '{"args":{"command":"tg --tag decision \"should we go with A or B?\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow","message":...} exit=0

# a complete body → allow, NO message:
echo '{"args":{"command":"tg --tag decision \"Context: the loader in app.py. Options: keep sync (simple, blocks) vs async (faster, complex). Recommendation: async.\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0

# a different tag → not a decision request, plain allow:
echo '{"args":{"command":"tg --tag report \"build green\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0

# the flag inside a string, not an argv flag → plain allow:
echo '{"args":{"command":"echo \"use --tag decision\""}}' \
  | ./decision_request_format.py; echo " exit=$?"        # → {"decision":"allow"} exit=0
```

A one-time silence is an external Telegram approval — set
`RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<why>"` (deny-by-default; only an approved
`tg-ctl ask` exit 0 silences the advisory).

The full suite is `tests/test_decision_request_format.py` in the repo root.
