# research-cli

A multi-provider **research / panel** CLI built on the shared `agenttools_providers`
engine. You ask a question; it puts that question to a *panel* of different models, each
through a research lens, then synthesizes their answers into one attributed note.

It is a **distinct tool, not a review-cli mode.** Code review and research are different
products; bolting research onto review as a mode would conflate them. But research needs
the *exact same* multi-model plumbing review-cli uses — board, failover, key cascade,
capability tags — which is why it waits on, and **reuses verbatim**, the shared providers
CORE (agent-tools#49). Tracking: [agent-tools#13](https://github.com/alex-mextner/agent-tools/issues/13).

```sh
research ask "What are the real trade-offs of a monorepo at 200 engineers?"
research board          # show the panel resolved against the shared model manifest
research ask --offline "…"   # run the whole pipeline with no key (stub transport)
```

## What it reuses vs what it adds

The shared `agenttools_providers` CORE is deliberately **network-free**: it decides
*which* seat / key to use; the consuming tool owns *how* to reach it. research-cli is that
consuming tool.

| Concern | Where it lives |
| --- | --- |
| Capability-tagged model **registry** + **role resolution** (`resolve_role`) | **reused** from `agenttools_providers` |
| Priority-ordered failover **Board** + pool/reserve split (`Board.split`) | **reused** from `agenttools_providers` |
| **Key cascade** (env beats `.env`, name precedence beats file order) | **reused** from `agenttools_providers` |
| Capability-tag **filtering** (`with_capability`) | **reused** from `agenttools_providers` |
| The shared **manifest** (`lib/contracts/models.yaml`) | **reused** — not forked |
| The **transport** (reachability predicate + the live call) | **added** here (`research_cli/transport.py`) — the half the CORE explicitly defers |
| The **research board** (analyst / skeptic / scout lenses) | **added** here — research lenses, not review's code-review lenses |
| The **panel pass** + synthesis | **added** here (`research_cli/engine.py`) |

The transport layer is exactly the seam the CORE's own README calls *"a thin transports
module that imports this CORE and adds the network"*. It is behind a small `Transport`
protocol, so the panel engine is fully unit-testable with an injected stub — no network in
any test.

## Architecture

```
bin/research                     # entry point -> research_cli.cli:main
research_cli/
  cli.py                         # self-registering command dispatcher (drop a file = a command)
  providers.py                   # the "reuse providers verbatim" bridge: imports the CORE,
                                 #   supplies the research board + per-provider key names
  transport.py                   # the DEFERRED half: reachability (via the CORE key cascade)
                                 #   + the live call. StubTransport (tests/offline) + Subprocess
  engine.py                      # the single-round panel pass + deterministic synthesis (MVP)
  commands/
    ask.py                       # research ask "<question>"  — the panel pass
    board.py                     # research board             — show the resolved board
```

Commands **self-register**: drop a `commands/<name>.py` exposing `NAME`, `SUMMARY`, and
`run(argv) -> int`, and it becomes `research <name>` with zero edits to the dispatcher.

## How a run works (single round — the MVP)

1. Resolve the failover **Board** against the shared registry (CORE `resolve_role`).
2. Use the transport's reachability as the CORE Board's availability **predicate**, so
   `board.split(pool, predicate)` returns the top-N reachable seats + a reserve. A seat
   unreachable at startup (no key for its provider) is **skipped** and the next-priority
   one **promoted**.
3. Ask each pooled seat the question through its lens. If a pooled seat **fails at call
   time**, the next reserve seat backfills it (mid-run failover). The CORE gives the
   order; the engine runs it.
4. **Synthesize** the answers into one Markdown note, each answer attributed to its
   concrete model + lens, with a footer noting how many answered and who was unavailable.

The MVP synthesis is **deterministic and offline** (a structured layout, not a model
call), so the output is testable and never itself a hallucination.

## Reachability and keys

A seat is reachable iff its provider's API key resolves through the shared **key cascade**
(env vars first, in name order, then `.env` files). The provider → key-name map is in
`research_cli/providers.py` (`PROVIDER_KEY_NAMES`). With no key for any seat, `research
ask` prints an empty-panel note and exits `69` (`EX_UNAVAILABLE`) — it never crashes.

To wire a **live** backend for the MVP, set `RESEARCH_BACKEND_CMD` to a shell template that
receives `{model}`, `{lens}`, `{question}` and prints the answer to stdout, e.g.:

```sh
export RESEARCH_BACKEND_CMD='opencode run --model {model} {question}'
research ask "…"
```

This is intentionally a thin shell-out for the MVP. A full transport (the `oc:` provider
router, `api|cli` mode selection, response parsing, sidecar logging) is the phased
follow-up below.

## Exit codes (structured)

| Code | Meaning |
| --- | --- |
| `0` | a synthesis was produced (≥1 seat answered) |
| `2` | usage error (no question, bad flag) |
| `69` | `EX_UNAVAILABLE` — no seat reachable / answered (no key, no backend) |
| `70` | `EX_SOFTWARE` — internal/config error (e.g. a malformed manifest) |

## Install (from the agent-tools umbrella checkout)

research-cli currently lives inside the agent-tools umbrella, next to the `lib/` it reuses
(see *Status* below). It runs straight from the checkout — `providers.py` puts `lib/` on
`sys.path`, so no install step is needed for the CORE:

```sh
# run directly:
tools/research-cli/bin/research ask "…"

# or install editable (pulls in the console-script `research` + PyYAML for the manifest):
pip install -e tools/research-cli[yaml]
research ask "…"
```

## Tests

```sh
# from the agent-tools repo root (the CI gate runs exactly this, pytest-only):
uv run --with pytest python -m pytest tests/test_research_cli.py -q
# the two manifest-loading tests additionally need pyyaml; they self-skip without it:
uv run --with pytest --with pyyaml python -m pytest tests/test_research_cli.py -q
```

Every test is deterministic and network-free: the transport is the network seam, so the
panel engine is driven by an injected `StubTransport` (no subprocess, no HTTP); registries
and boards are built from in-memory data, so only the single real-manifest smoke test
touches PyYAML (and `importorskip`s it, exactly like the providers suite). Coverage:
the providers-engine reuse (role resolution, the research board, the key cascade),
transport reachability + the live shell-out path (a real `printf` subprocess), the full
single-round panel pass (pool sizing, startup skip-and-promote, **mid-run failover
backfill**, empty-panel), the synthesis formatting, and the CLI dispatcher + `ask`/`board`
commands end-to-end.

## Roadmap (the phased rest)

The MVP is a single round + a deterministic synthesis — a genuinely useful multi-provider
answer, not a stub. Deferred, in priority order:

1. **A real transport** — the `oc:` / `opencode:` provider router, `api|cli` transport-mode
   selection, response parsing, timeouts, sidecar logging. (Today: one configurable
   `RESEARCH_BACKEND_CMD` shell template.)
2. **Multi-round** research — feed the round-1 synthesis back as follow-up questions to the
   panel, iterating until convergence or a round cap.
3. **Adversarial cross-examination** — have seats critique each other's answers (a debate
   pass) before synthesis, surfacing disagreement instead of averaging it away.
4. **Citation / source verification** — when seats cite sources, fetch and check them; flag
   unsupported claims. (Distinct from review-cli's quorum, which cites *code*.)
5. **Model-driven synthesis** — optionally ask a strong seat to reconcile the panel rather
   than the deterministic layout (kept deterministic in the MVP so the output is testable).

## Status — scaffolded inside the umbrella, to be spun out

The dedicated repo `alex-mextner/research-cli` does not exist yet, so per the tracking
issue (*"spin out a dedicated repo when work starts"*) this is scaffolded **inside
agent-tools**, next to the `lib/` it depends on. That makes the providers reuse trivial
to develop and test (one checkout, editable import, the umbrella CI gate runs the suite).

**To spin it out** when CTO sets up the repo: move `tools/research-cli/` to the new repo
root, drop the `sys.path` shim in `providers.py` (the package then depends on the published
`agenttools-providers` distribution declared in `pyproject.toml`), point the tests'
import shim at the installed package, and add it to the ecosystem catalog in the umbrella
README.

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain — the same providers engine
behind [review-cli](https://github.com/alex-mextner/review-cli) and task-cli's classifier.

## License

MIT.
