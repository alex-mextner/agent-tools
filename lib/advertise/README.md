# advertise — the shared `install-skill` core

Every agent-tools CLI ships a `<tool> install-skill` command that registers its **Agent
Skill** so harnesses auto-discover it. The mechanism is identical in `rig`, `review`, `tg`,
`draw`, `task`, `3d`, … and was copy-pasted ~4+ times. This package is that core, lifted
into one importable, well-tested library — **stdlib only**, no third-party deps.

## What `install-skill` does (the duplicated logic)

1. **Write `SKILL.md`** into `~/.agents/skills/<tool>/` — the Agent Skills standard
   location, read by Claude Code, Codex, opencode, Cursor. Idempotent: a
   byte-identical file is a no-op.
2. **Symlink** that skill dir into each **harness discovery dir** (claude-code:
   `~/.claude/skills/<tool>`). A skill that lives *only* in `~/.agents/skills` is invisible
   to a harness — the harness lists/loads from its own dir, so the link is what makes the
   tool actually appear.

## Safe-symlink discipline

Mirrors the most careful of the existing copies (rig's `riglib.install` + its
`link_skill_harness` apply action). For the harness link:

| Situation at the harness path        | What `advertise` does                                  |
| ------------------------------------ | ------------------------------------------------------ |
| nothing there                        | create the symlink                                     |
| **our** symlink, already correct     | no-op (`current`)                                      |
| a symlink pointing elsewhere / dangling | **re-point** it at the installed skill (`repointed`) |
| a **real** (non-symlink) dir/file    | governed by `on_conflict` (default: leave it untouched) |
| harness dir **is** the agents dir    | no self-link (`self`)                                  |
| `symlink_to` raises (no FS support…) | record the error, never raise (`error`)                |

This fixes two bugs the naive copies (`review`/`tg`/`draw`) carry: they link only when
`not link.exists()`, so (a) they never repair a stale/wrong link, and (b) a *dangling* link
reads as "exists" via `exists()` (which follows symlinks) and is left broken forever. Here
the test is `is_symlink()`, and a wrong link is always re-pointed.

The **SKILL.md write is the one load-bearing step**; every harness link is best-effort and
recorded on the result. A host without symlink support, or a hand-authored real dir in the
way, still gets the skill written and `install-skill` still reports the skill installed.

## API

```python
from advertise import install_skill

# Inline-string model (what rig / task / review use): pass the SKILL.md text.
result = install_skill("review", skill_md=REVIEW_SKILL_MD)

# Directory model: copy a whole skill dir (SKILL.md + sibling assets) in.
result = install_skill("openscad", source_dir="skills/openscad")

if not result:                      # falsy on an unresolved conflict / link error
    for line in result.summary_lines():
        print(line)
```

```python
def install_skill(
    tool: str,
    skill_md: str | None = None,
    *,
    source_dir: str | os.PathLike | None = None,   # XOR with skill_md
    skills_dir: str | os.PathLike = "~/.agents/skills",
    harness_dirs: Sequence[str | os.PathLike] = ("~/.claude/skills",),
    on_conflict: Literal["skip", "backup", "overwrite"] = "skip",
) -> InstallResult: ...
```

- Provide content **exactly one** of two ways — `skill_md=<text>` *or* `source_dir=<path>`
  (passing both, or neither, raises `ValueError`).
- `source_dir` is an **overlay copy**: it adds/refreshes files (preserving each file's mode
  bits, so a shipped `scripts/run.sh` stays executable via `shutil.copy2`) but does **not**
  prune destination files that disappeared from the source — a re-install never deletes what
  a prior install or the user left behind. A missing/invalid `source_dir` is validated
  **before** anything is written, so a bad call never leaves a half-made skill dir.
- `harness_dirs` may be a single path **or** a sequence; a bare string is treated as one
  dir (not iterated by character).
- `on_conflict` is validated up front — an invalid value raises `ValueError` rather than
  silently falling through to the destructive `overwrite` branch.
- `on_conflict` only governs a **real** dir/file already at a harness path (a wrong
  *symlink* is always re-pointed — it's ours to manage):
  - `"skip"` (default) — never clobber hand-authored content; the link isn't created and
    that harness link is reported as a `conflict`.
  - `"backup"` — move the real dir/file aside to `<name>.bak` (then `.bak.1`, …), then link.
  - `"overwrite"` — remove the real dir/file, then link.
- `~` paths are expanded; pass absolute paths (or override every dir) to make it testable
  under a tmp `$HOME`.

### Result

`install_skill` returns an `InstallResult`:

| Field / property      | Meaning                                                              |
| --------------------- | ------------------------------------------------------------------- |
| `tool`, `skill_dir`, `skill_md` | the tool name and the on-disk paths                       |
| `skill_status`        | `"written"` (created/updated) or `"current"` (idempotent no-op)     |
| `harness_links`       | a `HarnessLink` per harness dir (`status`, `link`, `ok`, `detail`)  |
| `result.ok` / `bool(result)` | the skill is written and every harness link is usable        |
| `result.changed`      | this run mutated the filesystem (wrote the skill or (re)made a link) |
| `result.summary_lines()` | human-readable per-target lines for a CLI to print               |

`HarnessLink.status` is one of `linked` / `current` / `repointed` / `self` / `conflict` /
`backed-up` / `overwritten` / `error`; `.ok` is False only for `conflict` and `error`.

## CLI (ad-hoc)

```sh
PYTHONPATH=lib python3 -m advertise <tool> --skill-md path/to/SKILL.md
PYTHONPATH=lib python3 -m advertise <tool> --source-dir path/to/skill-dir \
    --harness-dir ~/.claude/skills --on-conflict backup
```

Primary use is `from advertise import install_skill` inside a CLI's `install-skill` command;
the module entry point is for quick manual installs / relinks.

## How a CLI adopts it

Each CLI keeps its own `SKILL_NAME` / `SKILL_MD` constant (the content is tool-specific) and
delegates the mechanism:

```python
# <tool>lib/install.py
from advertise import install_skill

SKILL_NAME = "rig"
SKILL_MD = """---\nname: rig\n…"""

def install_skill_cmd() -> int:
    result = install_skill(SKILL_NAME, SKILL_MD)
    for line in result.summary_lines():
        print(line)
    return 0 if result.ok else 1
```

Wiring the CLIs to import this is a deliberate follow-up — **this package ships standalone**.
The blurb / harness-instruction-file / SessionStart-hook layers that `review`/`tg`/`draw`
add on top are a separate concern and intentionally **not** in this core; this module is the
SKILL.md-write + safe-harness-link piece that all of them share verbatim.

## Tests

`tests/test_advertise.py` runs under a tmp `$HOME`/`skills_dir`/`harness_dir`, so it never
touches the real `~/.agents/skills`. It covers fresh install, idempotent re-run, a wrong
symlink re-pointed, a dangling link repaired, a real dir/file left untouched (and the
`backup` / `overwrite` modes), the self-link guard, multi-harness, the `source_dir` copy
(content + executable bit + mode-only re-sync), argument guards (`on_conflict` validation,
the bare-string `harness_dirs` footgun, no orphan dir on a source error), the `_main` CLI
path, and best-effort link-failure recording.

```
$ uv run --with pytest python -m pytest tests/test_advertise.py -q
36 passed
```
