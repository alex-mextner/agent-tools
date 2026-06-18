---
name: decision-request-discipline
description: Use before escalating any product or architecture decision to a human (the project owner / CTO) — any "should we A or B", "open the PR or drop it", "which approach", or any mention of an open PR awaiting review. Brainstorm across models first and only escalate on genuine model disagreement or an irreversible high-blast-radius call; never relabel a product feature as a "risk/vuln/hazard"; and when you do escalate, send it to the human's channel in the strict question format so the decision takes 30 seconds without reading code.
---

# Decide what you can; escalate only what you can't — and only in the strict format

Asking a human to decide is expensive and slow. Most "decisions" an agent wants to
escalate are not decisions at all — they're things derivable from the code, or things a
model quorum already agrees on. The reflex to forward "A or B?" upward is usually offloading
work the agent should have done. Escalate **only** the genuinely open product/architecture
calls, and when you do, send a self-contained request that the human can answer in 30
seconds **without opening the repo** — not a bare "which one?".

## When to escalate (and when NOT)

Escalate **only** product/architecture decisions that are **not derivable from the code**.

- **Don't recommend — decide.** If you already know the right option, or can find it out,
  don't hand the choice up. **Brainstorm first** across multiple models (`review brainstorm`
  / a debate-swarm / `review quorum`). **If the model quorum converges with confidence, that
  IS the decision: implement it autonomously and report finished code + rationale (citing the
  quorum) — not a question.** Escalate ONLY on a real model disagreement, or an irreversible
  decision with a large blast radius and no clear winner. Forwarding a question the quorum
  already settled, as "pick A or B", is a violation.
- **Never relabel a product feature as a "risk / vuln / hazard".** A write the tool itself
  makes (e.g. retargeting an i18n key) is a *feature*, not a danger. A user or team seeing
  *their own* project data (their keys, texts, sources) is not a "leak". Before you call
  something a risk, ask: *who exactly is harmed, and how?* If there's no answer, it's not a
  risk — and dressing a normal feature up as a security concern to force a human's attention
  is the violation this clause exists to stop.
- **The open-PR trigger.** Any mention of an open PR in the human's channel or the chat —
  even "waiting for review" in a status report — counts as a decision request and runs this
  protocol. A PR sitting open *is* a "merge or not" question; treat it as one.

## Self-check before ANY question — derive, don't ask

Anything verifiable, you verify yourself. **If the answer is one shell command, do not ask.**

- `git log`, `git diff --stat`, `grep`, `curl`, `cat`, `git merge-tree` — run them yourself.
- Examples that are NEVER a question: *does the landing deploy from main?* → `grep` the
  workflows. *Is function X in main?* → `grep`. *Will these branches conflict?* →
  `git merge-tree`. *Is this dep transitive?* → read the manifest.

A question whose answer you could have grepped in ten seconds is the most common form of this
failure. Run the check first.

## Consult before you escalate

Before sending anything upward, exhaust the cheaper advisors:

1. Consult a peer-model advisor / `review` panel (whatever your harness exposes — an
   `advisor()` tool for the main agent, `codex exec` review via a subagent, or the `claude`
   CLI for a codex-style agent). Save a non-trivial review to `docs/reviews/` and commit it.
2. **The orchestrator does not post a decision request it drafted itself in passing.** A main
   agent juggling many tasks cuts corners — it ships a bare summary (options with no real
   pros/cons, no spec reference, no "where to look"). So the request is **drafted by a
   subagent** whose only job is to read the spec/code and assemble it strictly to the format
   below. This is a hard rule, not a preference.
3. **Self-check before sending** (any agent, every time): does the message have Context + a
   glossary of internal terms + Options with REAL pros/cons + a Recommendation + "where to
   look"? If even one is missing, it is malformed — do not send; rewrite it.
4. Only if the question is still open after all that — escalate.

## The channel: the human's out-of-band channel (mandatory)

Every decision request **must** go to the human's real channel (e.g. Telegram via the `tg`
CLI), in parallel with any chat post, immediately when it's formulated. The human does not
watch the agent's chat. "I'll remind them later" is not a channel.

**One consolidated list on request.** When the human asks "what are you waiting on me for" /
"all the open decisions", reply with a **single message listing ALL currently-open decisions**,
each in the format below (concrete question + options with real pros/cons + recommendation) —
not a vague category ("need to decide D1/D4/D5") and not scattered fragments. Keep it current:
mark resolved ones closed; don't re-list them as open.

## The question format (8 points — the human decides in 30s without reading code)

1. **Context** — where the code is (`file:line`), what the function does.
2. **Glossary of terms and names** — every internal name (ticket, module, function,
   acronym, flag) explained in one phrase. The human is not obliged to remember your jargon.
3. **Problem** — what concretely needs deciding (not abstractly).
4. **Options** — only ones that each have a real advantage. Each option gets **meaningful,
   reasonable pros / cons** — the kind that actually move the choice, not filler.
5. **Recommendation** — which option you think is right, and why.
6. **Where to look in the code** — the specific files and lines for a fast review.
7. **Screenshots** — if the problem is visual: BEFORE (the problem) + a description of the
   intended AFTER.
8. **Pseudocode for any non-trivial mechanism** — 3–7 lines of "before / after" instead of a
   paragraph of prose; a short snippet is clearer than the paragraph.

**More context is better than a question that can't be answered without opening the repo.**

## Showing a child-repo diff: formatted Telegram text, NEVER a raw `.patch`

The hard case is a change **not in the main repo** the human reviews locally, but in a
**child / sibling project** — another repo, worktree, extension, or CLI utility. The human
does not have that code checked out and will not go hunting for it, least of all from a phone.
So:

- Generate an authoritative diff **from the child repo's branch worktree**, basing on the
  PR's *own* base (a hardcoded `origin/main` gives a wrong diff for a PR based on `develop`):
  ```sh
  BASE=$(gh pr view --json baseRefName -q .baseRefName)
  git diff "origin/$BASE...HEAD"   # three dots; more reliable than `gh pr diff <N>`
  ```
- **Send the diff as TEXT in the channel, nicely formatted — NEVER as a raw `tg --file
  <patch>`.** A `.patch` is unreadable on a phone. Use `tg --format html`, each diff chunk in
  a `<pre><code class="language-diff">…</code></pre>` block; long / secondary chunks in a
  `<blockquote expandable>`. Escape raw `<`, `>`, `&` inside the diff as `&lt;` `&gt;` `&amp;`.
- The channel caps a message near 4096 chars — **split by file / hunk** across messages.
  Send the mechanism core (production code) first; summarize large test diffs in a line
  rather than dumping them. Prefix each block with a "where to look" line: `file — purpose`.
- **A tool that EDITS someone else's code (not its own repo) — show the diff of the RESULT in
  the edited project.** If the feature is an extension/CLI that rewrites a target (user /
  fixture / child) project's files, the human checks **what was written into the target file**
  — not the tool's own diff. Capture the target file's `before → after` (grab it from the e2e
  run before the test restores the fixture) and confirm **no other files changed**
  (`git status` in the target project).
- **Mandatory `tg` bug workarounds (without these the diff send fails or arrives as garbage):**
  - Run `tg` from a **neutral cwd** (`cwd: /tmp`). `tg` auto-detects real file paths in the
    text and tries to send them as a media group (1024-char caption cap) → a diff (full of
    paths) fails with `400`. From `/tmp`, relative repo paths don't resolve → it goes as clean
    text.
  - Set `TG_AI_MODEL=claude` in the call's env — otherwise the auto-sender label emits garbage.
  - Keep each chunk **≤ ~3000 chars of raw body** (HTML-escaping `<`/`>`/`&` inflates length;
    leave headroom under 4096). Send sequentially, marked `(i/N)`.
- For the **main** repo (the human reviews it locally), a PR link + a per-item summary is
  enough — but if they ask to see the code, send it the same way: formatted text, not a file.

## "PR or drop" / "open a PR or close it" decisions

Never write just "need to: open a PR or drop". That's offloading the decision with no context.
A PR-or-drop request must state:

- What is concretely implemented (the functionality, not a file list).
- The linked ticket and its priority.
- Whether it's still relevant on the current roadmap (as far as code + history show).
- What's lost on drop (effort, uniqueness of the approach).
- The risks on merge (conflicts, regressions, test coverage).

## Common mistakes

- **Forwarding a quorum-settled question.** The models already agreed; you turned it into "A
  or B?" anyway. Implement and report, don't ask.
- **A grepable question.** "Does it deploy from main?" when one `grep` answers it. Self-check
  first.
- **A bare "which one?".** Options with no real pros/cons, no recommendation, no "where to
  look" — the human can't answer without opening the repo. That's a malformed request.
- **Risk-washing a feature.** Calling a normal product behavior a "vuln/leak/hazard" to force
  attention. Name the concrete harm, or don't call it a risk.
- **Raw `.patch` to the channel.** A file the human can't read on a phone. Send formatted diff
  text in chunks.
- **The orchestrator drafting its own request.** It cuts corners under load; delegate the
  drafting to a subagent that reads the spec/code.

## Why

The whole point of escalation is that it's rare and high-signal. If the agent escalates
everything derivable, or sends bare "A or B?" questions, the human either becomes a bottleneck
or stops reading. A decision request that a quorum could have settled wastes a human round-trip;
one missing context wastes two (the question, then the "I need more info"). Decide what you can,
and when you genuinely can't, make the one escalation count.

Pairs with `adversarial-verification` (before you call a finding a "risk", construct the harm —
or admit there isn't one), `delegate-work-to-subagents` (the orchestrator dispatches the
request-drafting, it doesn't do it inline), and `queued-report-durability` (if the channel send
fails, surface it and re-queue — never silently treat the escalation as delivered).
