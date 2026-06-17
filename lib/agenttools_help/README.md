# agenttools-help

One **shared help formatter** for every CLI in the agent-tools ecosystem. `tg`'s `--help` was
**not** colorized like `review` / `rig` / `draw` — inconsistent — and every tool
re-implemented section layout, the usage line, and the subcommands list slightly differently.
This is the ONE shared help layer the roadmap calls for: build help as **data** here and a fix
(or a new help-clarity rule) lands across every CLI at once.

**Stdlib only.** Zero runtime dependencies — `dataclasses` + `os`/`sys`/`shutil`/`textwrap`
(plus optional `argparse` introspection). So `--help` stays fast and offline.

## What it standardizes (the help-clarity rules, one place)

- **Colors / styling** — one palette (`Palette`), semantic roles (title/heading/option/
  subcommand/topic/default/ok/warn/err). Change the scheme here → the whole ecosystem matches.
- **Section layout + usage line(s) + subcommands list** — described as data, rendered (aligned,
  wrapped, colored) by the formatter.
- **ACTUAL defaults** — `Option(default=…)` shows the real value, e.g. pass the *resolved*
  `--model` so help reflects what will really run, not a stale literal. A switch with no
  default renders **no** `(default: …)` annotation (no misleading "default: none"). To express
  "no default", **omit** the `default=` kwarg entirely (both `Section.add(...)` and
  `Option(...)` default it to an internal sentinel) — passing `default=None` is a *real* `None`
  default and renders `(default: none)`.
- **Scoped subcommand options** — a subcommand's options live in a `Section(scope="run")` and
  render under `options for \`run\`:`, so they physically can't leak into the global block.
- **Topic-help** (`<tool> help <topic>`) — `TopicRegistry` replaces bespoke `--format-help`
  flags; the main `--help` advertises the topic names, `help <name>` prints the full body.
- **Install / setup state** — `InstallState` + the `✓` / `○` / `⚠` glyphs the roadmap wants on
  every install-/setup-/voice-/completion-state surface (configured / pending / conflict).

## Quick start

```python
from agenttools_help import HelpFormatter, InstallState, TopicRegistry

topics = TopicRegistry().add(
    "format", "text vs html output", "tg renders plain text by default …"
)

hf = HelpFormatter(prog="tg", tagline="send Telegram messages from any agent")
hf.add_usage("tg [global options] <message>")
hf.add_usage("tg help <topic>")

g = hf.add_section("global options")
g.add("-f", "--format", metavar="FMT", help="output format",
      choices=["text", "html"], default="text")         # ACTUAL default shown — once

voice = hf.add_section("options", scope="voice setup")   # scoped to a subcommand
voice.add("--whisper-model", metavar="NAME", help="local whisper model", default="base")

hf.add_subcommand("voice", "voice-reply setup + transcription")
hf.add_install_state(InstallState.configured("voice setup", "whisper at ~/.cache/whisper"))
hf.add_install_state(InstallState.not_configured("zsh completion"))
hf.topics = topics

print(hf.render())                                  # <tool> --help
print(topics.render_topic("format", prog="tg"))     # <tool> help format
```

Renders (plain):

```
tg — send Telegram messages from any agent

usage:
  tg [global options] <message>
  tg help <topic>

commands:
  voice  voice-reply setup + transcription

global options:
  -f, --format FMT  output format (choices: text, html) (default: text)

options for `voice setup`:
  --whisper-model NAME  local whisper model (default: base)

setup:
  ✓ voice setup — whisper at ~/.cache/whisper
  ○ zsh completion

help topics:
  format  text vs html output

  see `tg help <topic>`, e.g. `tg help format`
```

## Reuse what's already declared: `options_from_argparse`

A CLI that already declares its flags in `argparse` can harvest them instead of re-listing:

```python
from agenttools_help import HelpFormatter, options_from_argparse

hf = HelpFormatter(prog="rig")
hf.sections.append(options_from_argparse(parser, scope="apply"))
```

It reads each action's option strings, metavar, help, **default** (value options only — a
`store_true` switch gets no default), and choices; the `-h/--help` action is skipped.

## Color

`render(color=…)` forces it; otherwise `should_color(stream)` decides: `NO_COLOR` (any
**non-empty** value) disables,
`FORCE_COLOR` forces on, else color only on a real TTY — same precedence as `agenttools_errors`
so an error and the help around it look identical.

## Tests

```
uv run --with pytest python -m pytest tests/test_agenttools_help.py -q
```
