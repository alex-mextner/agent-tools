# agenttools-tmux-inject

Inject text/keys into a named tmux **pane or session** via `tmux send-keys` — **stdlib
only** (no `libtmux`, no third-party dep). The use case: a finishing agent posts a line
into *another* agent's interactive pane. For example task-cli, on completing a task, types
`done, X unblocked` into the pane of the agent that was blocked on it, so that agent's
REPL actually receives and processes the message.

Extracted from `tg-ctl` (which already injects inbound messages into an agent's pane) and
lifted into a shared library so task-cli, tg-ctl, and any future tool inject identically.

## Usage

```python
from agenttools_tmux_inject import inject, has_session

# The headline call: type the text and press Enter.
if has_session("work"):
    result = inject("work:1.0", "done, deploy unblocked")
    if not result.ok:
        log.warning("could not notify pane", error=result.error, msg=result.message)
```

`inject(target, text, *, enter=True, literal=True)` is the common case. It runs
`tmux send-keys -t <target> -l '' <text>` to type the text literally, then — when
`enter=True` (the default) — a *second*, interpreted `tmux send-keys -t <target> Enter`
to press a real Return so the receiving program processes the line. (The empty `''` is an
empty literal key arg that guards a dash-leading `<text>` from being misparsed as a flag —
`tmux send-keys` has no `--` end-of-options marker; see the Literal vs interpreted note.)

### Targets

A `target` is either a tmux **pane id** or a **`session:window.pane` address** — both are
passed verbatim to tmux, whose own target grammar is the source of truth:

| Target          | Meaning                                  |
| --------------- | ---------------------------------------- |
| `%3`            | pane id `3` (server-unique, rename-safe) |
| `work`          | session `work` (active window/pane)      |
| `work:1`        | session `work`, window `1`               |
| `work:1.0`      | session `work`, window `1`, pane `0`     |
| `:1.0`          | current session, window `1`, pane `0`    |

`resolve_target(s)` parses one into a `Target` (with `.is_pane_id`, `.session`, `.window`,
`.pane`) for inspection; it preserves the original string in `.raw`, which is what actually
reaches tmux.

### Literal vs interpreted, and Enter

| Flag             | `send-keys` form         | Effect                                              |
| ---------------- | ------------------------ | --------------------------------------------------- |
| `literal=True`   | `send-keys -l '' TEXT`   | bytes verbatim — `Enter`/`C-c` in the text are typed as text (default) |
| `literal=False`  | `send-keys '' KEYS`      | argument interpreted as tmux key *names* (`C-c`, `Enter`, `Escape`) |
| `enter=True`     | extra `send-keys Enter`  | presses a **real** Return after the text (default for `inject`) |
| `enter=False`    | *(none)*                 | leaves the text on the prompt, unsent               |

The empty `''` leading key arg is deliberate: `tmux send-keys` does **not** accept a `--`
end-of-options marker (its documented synopsis is `send-keys [-FHKlMRX] … key …`), so an
empty literal key is prepended to guard a payload that itself begins with `-` (`--help`,
`-rf /tmp`) from being parsed as a flag. It sends no characters of its own.

Enter is deliberately a separate, interpreted `send-keys Enter` call — *not* a `"\n"`
appended to the literal text send. Under `-l` a trailing newline is a literal LF byte,
which many readline shells treat differently from a real Return.

### Low-level primitive

```python
from agenttools_tmux_inject import send_keys

# Send a Ctrl-C as an interpreted key name (no Enter, not literal):
send_keys("work:1.0", "C-c", literal=False)
```

`send_keys` is what `inject` is built on. It defaults `enter=False` so the call maps 1:1
to a single `send-keys`; opt into the trailing Return with `enter=True`.

### Enumerating panes

```python
from agenttools_tmux_inject import list_panes

for p in list_panes("work"):          # scope to a session (or omit for all panes)
    print(p["pane_id"], p["session"], p["window_index"], p["active"], p["title"])
```

`list_panes` runs `tmux list-panes -F <format>` and parses the tab-separated output into
dicts (`pane_id`, `session`, `window_index`, `window_name`, `pane_index`, `active`,
`title`). Empty list when tmux/server/target is absent.

## Result, never an exception

Every call returns an `InjectResult` (or, for `has_session`/`list_panes`, a bool/list).
Injecting into another agent's pane is a best-effort side-channel, so **runtime/environment
failures degrade to a result, they do not raise** — safe to call straight from a completion
hook without a try/except:

| Field        | Meaning                                                        |
| ------------ | ------------------------------------------------------------- |
| `ok`         | `True` only when tmux exited 0 (also `bool(result)`)           |
| `error`      | `None` on success, else an `ERR_*` sentinel                    |
| `message`    | human-readable explanation on failure                         |
| `argv`       | the command(s) that were (or would have been) run — see below  |
| `returncode` | tmux's exit code (when it ran)                                 |
| `stderr`     | tmux's stderr (when it ran)                                    |

Error sentinels: `ERR_NO_TMUX` (binary not on PATH), `ERR_NO_SERVER` (no server running),
`ERR_BAD_TARGET` (pane/session not found), `ERR_SEND_FAILED` (send-keys ran non-zero),
`ERR_TIMEOUT` (tmux hung past the `timeout`).

`argv` is a faithful log record, not a single shell-ready line: an `enter=True` injection
runs **two** `send-keys` invocations (literal text, then interpreted `Enter`) and their
argvs are stored **concatenated** into one flat sequence. Split on the tmux binary if you
need the individual commands.

What **does** raise: programmer errors — a non-string `text`/`keys`, an empty or
non-string target/session name. Those are bugs, not environment conditions.

The tmux binary is resolved via `shutil.which("tmux")`; override with the
`AGENTTOOLS_TMUX_BIN` env var (a bare name on PATH or an absolute path) for odd installs.

## Public API

| Symbol | Purpose |
| --- | --- |
| `inject(target, text, *, enter=True, literal=True, timeout=5.0) -> InjectResult` | type text + press Enter |
| `send_keys(target, keys, *, literal=True, enter=False, timeout=5.0) -> InjectResult` | low-level send-keys |
| `has_session(name, *, timeout=5.0) -> bool` | does the session exist |
| `list_panes(target=None, *, timeout=5.0) -> list[dict]` | enumerate panes |
| `resolve_target(target) -> Target` | parse a target string |
| `InjectResult` / `Target` | result and parsed-target dataclasses |
| `ERR_NO_TMUX` / `ERR_NO_SERVER` / `ERR_BAD_TARGET` / `ERR_SEND_FAILED` / `ERR_TIMEOUT` | error sentinels |

## Installing / importing as a consumer

The package lives under `lib/agenttools_tmux_inject/` in the umbrella repo and builds as
the `agenttools-tmux-inject` distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-tmux-inject"]
```

```sh
pip install -e /path/to/agent-tools/lib/agenttools_tmux_inject
# or ad-hoc with uv:
uv run --with /path/to/agent-tools/lib/agenttools_tmux_inject \
  python -c "from agenttools_tmux_inject import inject"
```

```python
from agenttools_tmux_inject import inject
inject("work:1.0", "done, X unblocked")
```
