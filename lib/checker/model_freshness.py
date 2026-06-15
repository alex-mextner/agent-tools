#!/usr/bin/env python3
"""model-freshness — propose model-version bumps for lib/contracts/models.yaml.

Accessed via: the daily cron rig provisions (`rig` installs a launchd plist on macOS / a
  crontab line on Linux that runs `python3 .../lib/checker/model_freshness.py`). Also
  runnable by hand and imported by tests (`from checker.model_freshness import run`).

What it does (semi-automatic, PROPOSE-never-merge):
  1. Reads lib/contracts/models.yaml — the current per-provider pins.
  2. For each provider that exposes a model-list endpoint (OpenAI /v1/models,
     Anthropic /v1/models, Google generativelanguage, commandcode + z.ai
     OpenAI-compatible /models), polls it using a key harvested from the existing
     CLI/env configs (env vars first, then the same `.env` fallback files review-cli
     reads). A provider whose key is ABSENT is skipped — never a crash.
  3. When the live endpoint advertises a NEWER version than the manifest pins for that
     provider/role, it PROPOSES a bump: opens a PR against agent-tools editing
     models.yaml (via `gh`) with the diff + the new model's advertised capabilities;
     OR, if `gh`/auth is absent, writes a dated report to lib/checker/reports/.
  4. Idempotent: if a proposal PR for the same bump is already open, it does not open a
     duplicate; the report path is deterministic per (provider, new id) so a re-run
     overwrites rather than piling up.

Stdlib-first: only the standard library is imported at module load. `yaml` is imported
lazily inside the manifest loader (the manifest is YAML); `urllib.request` does the HTTP.
No third-party HTTP client, no provider SDKs.

Safety posture: a newer model can REGRESS (a "turbo" variant may drop vision, a point
release may be cheaper-but-worse). So the checker never edits the manifest in place and
never merges — a human confirms every bump. The capabilities of the proposed model are
surfaced in the proposal so the human can see, e.g., that the candidate lost `vision`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ── paths ──────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent / "contracts"
MANIFEST_PATH = _CONTRACTS / "models.yaml"
SCHEMA_PATH = _CONTRACTS / "models.schema.json"
REPORTS_DIR = _HERE / "reports"
# The agent-tools checkout root (lib/checker/.. -> lib/.. -> repo root). `gh` runs here.
REPO_ROOT = _HERE.parent.parent

# The capability set the manifest may use (mirrors models.schema.json `capability` enum —
# kept in sync by the schema-conformance test).
KNOWN_CAPABILITIES = ("vision", "code", "reasoning", "tools", "embeddings", "audio")

# Sentinel woven into a proposal PR title/branch so a re-run can find an existing one and
# stay idempotent (no duplicate PRs for the same bump).
PROPOSAL_MARKER = "model-freshness"


# ── key resolution (harvest from existing CLI/env config — never hardcode) ──────────────
# The same `.env` fallback files review-cli reads, so a key configured once for review is
# reused here. Env vars always win; files are a fallback. NEVER hardcode a key.
def _fallback_files() -> tuple[Path, ...]:
    """The .env fallback files to search, computed LAZILY (so a test's HOME monkeypatch
    takes effect, and ``MODEL_FRESHNESS_ENV_FILE`` can override for ad-hoc use)."""
    override = os.environ.get("MODEL_FRESHNESS_ENV_FILE")
    if override:
        return (Path(override),)
    return (
        Path.home() / ".config" / "review-cli" / ".env",
        Path.home() / ".config" / "rig" / ".env",
    )


def _read_env_key(env_file: Path, var: str) -> str | None:
    """Read VAR=value from a flat .env file (quotes stripped). None on miss/unreadable."""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def resolve_key(env_names: tuple[str, ...]) -> str | None:
    """Resolve a provider key from the env (in order), then the .env fallback files.

    Key-name-first precedence (mirrors review-cli): an env var beats every file, and among
    the files the first accepted name wins regardless of which file it lives in. Returns
    None when no name resolves anywhere — the caller SKIPS that provider, never crashes.
    """
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    files = _fallback_files()
    for name in env_names:
        for path in files:
            key = _read_env_key(path, name)
            if key:
                return key
    return None


# ── version parsing / comparison ────────────────────────────────────────────────────────
# A "version" is the numeric run extracted from a model id within the SAME family. We never
# compare across families (gpt-5.5 vs gemini-2.5 is meaningless); the checker only asks
# "is there a NEWER version of the family this provider's pin belongs to?".
#
# Endpoints commonly return DATED / build-suffixed snapshots: Anthropic `/v1/models` gives
# `claude-opus-4-8-20260101`, Gemini `gemini-2.5-flash-002`, OpenAI `gpt-5.5-2026-01-01`.
# A naive "strip all digit runs" makes those a DIFFERENT family than the bare pin, so no bump
# would ever be proposed for those providers (the checker's whole point). So we FIRST strip a
# trailing date/build suffix, THEN derive the family skeleton + the comparable version tuple.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")
# A trailing snapshot/date/build suffix to ignore when deriving the family + version:
#   -YYYYMMDD (8 digits), -YYYY-MM-DD, -NNN / -NNNN (a 3-4 digit build like Gemini's -002),
#   -preview / -latest / -exp markers. Anchored at end; only the FIRST match is stripped.
_SNAPSHOT_SUFFIX_RE = re.compile(
    r"(?:-(?:\d{8}|\d{4}-\d{2}-\d{2}|\d{3,4})|-(?:preview|latest|exp|beta|alpha)(?:-[0-9a-z]+)?)$",
    re.IGNORECASE,
)


def strip_snapshot_suffix(model_id: str) -> str:
    """Drop a trailing dated/build/preview snapshot suffix from an id.

    `claude-opus-4-8-20260101` -> `claude-opus-4-8`, `gemini-2.5-flash-002` -> `gemini-2.5-flash`,
    `gpt-5.5-2026-01-01` -> `gpt-5.5`, `gpt-5.6-preview` -> `gpt-5.6`. Applied once; an id with
    no such suffix is returned unchanged. This is what lets a dated endpoint snapshot match the
    bare pin's family and so be considered as a bump.
    """
    return _SNAPSHOT_SUFFIX_RE.sub("", model_id)


def model_family(model_id: str) -> str:
    """The family skeleton of a model id: snapshot-suffix stripped, then every numeric run
    replaced by a single ``#`` placeholder.

    `gpt-5.5` -> `gpt-#`, `gpt-5.6` -> `gpt-#`, `claude-opus-4-8` -> `claude-opus-#-#`,
    `claude-opus-4-8-20260101` -> `claude-opus-#-#`, `gemini-2.5-flash-002` -> `gemini-#-flash`,
    `moonshotai/Kimi-K2.7-Code` -> `moonshotai/Kimi-K#-Code`. Two ids share a family iff this
    skeleton matches — so only same-family ids are ever version-compared, and a dated snapshot
    matches its bare pin. (A different SEGMENT COUNT — `claude-opus-4-8` vs `claude-opus-5` —
    yields different skeletons `…-#-#` vs `…-#`; a deliberate next-major like that is NOT
    auto-proposed, which is the safe default — a human adds the new family entry.)
    """
    return _VERSION_RE.sub("#", strip_snapshot_suffix(model_id))


def version_tuple(model_id: str) -> tuple[int, ...]:
    """The comparable version tuple of an id: snapshot-suffix stripped, then ALL numeric runs
    in order.

    `gpt-5.5` -> (5, 5); `claude-opus-4-8` -> (4, 8); `claude-opus-4-8-20260101` -> (4, 8)
    (the date suffix is dropped first, so a re-dated SAME version doesn't read as newer);
    `gemini-2.5-flash-002` -> (2, 5). An id with no digits sorts lowest (`()`). Stays
    consistent with `model_family` (both strip the snapshot suffix first), so within a family
    this tuple orders the ids. Comparison across families is meaningless and guarded by
    `is_newer`.
    """
    base = strip_snapshot_suffix(model_id)
    return tuple(int(run) for run in re.findall(r"\d+", base))


def is_newer(candidate: str, current: str) -> bool:
    """True iff `candidate` is the SAME family as `current` but a strictly higher version.

    Cross-family ids (different skeletons) are never "newer" — that prevents proposing
    `gemini-2.5` as a bump for `gpt-5.5`, and prevents a next-MAJOR with a different segment
    shape from being auto-proposed (a human adds that). A dated snapshot of the SAME version
    (`gpt-5.5-20260101` vs pin `gpt-5.5`) is NOT newer (same version tuple after stripping the
    date) — so re-dated republishes of the current model don't churn proposals. A genuinely
    newer same-family release (`gpt-5.6`, `gpt-5.6-20260201`) IS newer.
    """
    if model_family(candidate) != model_family(current):
        return False
    return version_tuple(candidate) > version_tuple(current)


# ── manifest model ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider: str
    capabilities: tuple[str, ...]
    context: int | None = None
    notes: str = ""


@dataclass
class Manifest:
    version: int
    models: list[ModelEntry]
    roles: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def by_provider(self, provider: str) -> list[ModelEntry]:
        return [m for m in self.models if m.provider == provider]

    def providers(self) -> list[str]:
        seen: list[str] = []
        for m in self.models:
            if m.provider not in seen:
                seen.append(m.provider)
        return seen

    def entry(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)


class ManifestError(ValueError):
    """The manifest is malformed or violates a cross-reference invariant."""


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest:
    """Parse + lightly-structure models.yaml. Lazy yaml import (manifest is the only YAML).

    Raises ManifestError on a non-mapping / missing-required shape — fail loud, since a
    silently-empty manifest would make the checker "find" everything as new.
    """
    try:
        import yaml  # lazy: keeps the module stdlib-only at IMPORT time
    except ImportError as exc:
        # PyYAML is the checker's one runtime dep. The cron runs `python3 model_freshness.py`,
        # so a machine without it must get a clear, actionable error — not a raw traceback.
        raise ManifestError(
            "PyYAML is required to read the manifest but is not installed. "
            "Install it: `python3 -m pip install --user pyyaml` (or via your package "
            "manager). rig provisions it as a dependency (`rig doctor`)."
        ) from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} must be a mapping")
    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ManifestError("manifest `models:` must be a non-empty list")
    models: list[ModelEntry] = []
    for entry in models_raw:
        if not isinstance(entry, dict):
            raise ManifestError(f"model entry not a mapping: {entry!r}")
        mid = entry.get("id")
        provider = entry.get("provider")
        caps = entry.get("capabilities")
        if not isinstance(mid, str) or not mid.strip():
            raise ManifestError(f"model entry missing 'id': {entry!r}")
        if not isinstance(provider, str) or not provider.strip():
            raise ManifestError(f"model {mid!r} missing 'provider'")
        if not isinstance(caps, list) or not caps:
            raise ManifestError(f"model {mid!r} missing 'capabilities'")
        ctx = entry.get("context")
        models.append(
            ModelEntry(
                id=mid.strip(),
                provider=provider.strip(),
                capabilities=tuple(str(c) for c in caps),
                # `isinstance(True, int)` is True, so guard against `context: true` silently
                # becoming 1 — only a real (non-bool) int is a context window.
                context=int(ctx) if isinstance(ctx, int) and not isinstance(ctx, bool) else None,
                notes=str(entry.get("notes", "")),
            )
        )
    roles = {str(k): str(v) for k, v in (raw.get("roles") or {}).items()}
    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}
    return Manifest(
        version=int(raw.get("version", 1)),
        models=models,
        roles=roles,
        aliases=aliases,
    )


def validate_manifest(manifest: Manifest) -> list[str]:
    """Return a list of human-readable problems ([] == valid).

    Enforces the cross-references JSON-Schema cannot:
      - every role/alias target is a concrete id present in `models:`;
      - the `vision` role/alias resolves ONLY to a vision-capable entry (#3681);
      - capabilities are drawn from the known set;
      - `<provider>:latest` points at an entry of THAT provider.
    """
    problems: list[str] = []
    ids = {m.id for m in manifest.models}
    for m in manifest.models:
        unknown = set(m.capabilities) - set(KNOWN_CAPABILITIES)
        if unknown:
            problems.append(f"model {m.id!r}: unknown capabilities {sorted(unknown)}")

    def _check_pointer(label: str, key: str, target: str) -> None:
        if target not in ids:
            problems.append(f"{label} {key!r} -> {target!r} which is not a concrete model id")
            return
        if key == "vision" or key.endswith(":vision"):
            entry = manifest.entry(target)
            if entry and "vision" not in entry.capabilities:
                problems.append(
                    f"{label} 'vision' -> {target!r} but that model is NOT vision-capable "
                    f"(capabilities: {list(entry.capabilities)}) — violates #3681"
                )
        if key.endswith(":latest"):
            provider = key.split(":", 1)[0]
            entry = manifest.entry(target)
            if entry and entry.provider != provider:
                problems.append(
                    f"{label} {key!r} -> {target!r} but that model's provider is "
                    f"{entry.provider!r}, not {provider!r}"
                )

    for key, target in manifest.roles.items():
        _check_pointer("role", key, target)
    for key, target in manifest.aliases.items():
        _check_pointer("alias", key, target)
    return problems


# ── provider polling (OpenAI-compatible /models, Anthropic, Google) ─────────────────────
@dataclass
class PollResult:
    provider: str
    ok: bool
    model_ids: list[str] = field(default_factory=list)
    skipped: str = ""  # reason, when ok is False and it's a skip (no key) not an error
    error: str = ""


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https hosts
        body = resp.read().decode("utf-8", "replace")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _ids_from_openai_list(data: dict[str, Any]) -> list[str]:
    """OpenAI-compatible /models returns {data: [{id: ...}, ...]}."""
    out: list[str] = []
    for item in data.get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append(item["id"])
    return out


def _ids_from_gemini_list(data: dict[str, Any]) -> list[str]:
    """Google generativelanguage returns {models: [{name: 'models/gemini-...'}, ...]}."""
    out: list[str] = []
    for item in data.get("models", []) or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"].split("/")[-1])
    return out


# Per-provider polling config: env key names + how to build the request + parse the ids.
# A provider absent here has no model-list endpoint (or we don't poll it) — it's skipped.
def _poll_openai(timeout: float) -> PollResult:
    key = resolve_key(("OPENAI_API_KEY",))
    if not key:
        return PollResult("openai", False, skipped="no OPENAI_API_KEY")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    try:
        data = _http_get_json(f"{base}/v1/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("openai", False, error=str(exc))
    return PollResult("openai", True, model_ids=_ids_from_openai_list(data))


def _poll_anthropic(timeout: float) -> PollResult:
    key = resolve_key(("ANTHROPIC_API_KEY",))
    if not key:
        return PollResult("anthropic", False, skipped="no ANTHROPIC_API_KEY")
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        data = _http_get_json(f"{base}/v1/models", headers, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("anthropic", False, error=str(exc))
    return PollResult("anthropic", True, model_ids=_ids_from_openai_list(data))


def _poll_gemini(timeout: float) -> PollResult:
    key = resolve_key(("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    if not key:
        return PollResult("gemini", False, skipped="no GEMINI_API_KEY")
    base = os.environ.get(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    try:
        # Send the key as a HEADER (x-goog-api-key), not a `?key=` query param — a URL with
        # the key embedded can leak into an error string / log. Google accepts both.
        data = _http_get_json(f"{base}/v1beta/models", {"x-goog-api-key": key}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("gemini", False, error=str(exc))
    return PollResult("gemini", True, model_ids=_ids_from_gemini_list(data))


def _poll_commandcode(timeout: float) -> PollResult:
    key = resolve_key(("COMMANDCODE_API_KEY",))
    if not key:
        return PollResult("commandcode", False, skipped="no COMMANDCODE_API_KEY")
    base = os.environ.get(
        "COMMANDCODE_BASE_URL", "https://api.commandcode.ai/provider/v1"
    ).rstrip("/")
    try:
        data = _http_get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("commandcode", False, error=str(exc))
    return PollResult("commandcode", True, model_ids=_ids_from_openai_list(data))


def _poll_zai(timeout: float) -> PollResult:
    key = resolve_key(("ZAI_API_KEY", "ZHIPU_API_KEY"))
    if not key:
        return PollResult("zai", False, skipped="no ZAI_API_KEY")
    base = os.environ.get(
        "ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4"
    ).rstrip("/")
    try:
        data = _http_get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PollResult("zai", False, error=str(exc))
    return PollResult("zai", True, model_ids=_ids_from_openai_list(data))


# fireworks has no stable public model-list contract here; it routes through commandcode in
# practice, so we don't poll it directly (skipped with a clear reason).
def _poll_fireworks(timeout: float) -> PollResult:
    return PollResult("fireworks", False, skipped="no direct model-list endpoint (routed)")


POLLERS: dict[str, Callable[[float], PollResult]] = {
    "openai": _poll_openai,
    "anthropic": _poll_anthropic,
    "gemini": _poll_gemini,
    "commandcode": _poll_commandcode,
    "zai": _poll_zai,
    "fireworks": _poll_fireworks,
}


# ── proposal computation ────────────────────────────────────────────────────────────────
@dataclass
class Proposal:
    provider: str
    current_id: str
    new_id: str
    # capabilities we can INFER for the new id: by default the same as the current pin's
    # (the endpoint rarely advertises capability flags). The human verifies — this is why
    # bumps are proposed, never auto-merged: a "turbo" variant can silently drop vision.
    inferred_capabilities: tuple[str, ...]
    note: str = ""

    @property
    def slug(self) -> str:
        """A filesystem/branch-safe slug unique per (provider, new id) — drives idempotency."""
        raw = f"{self.provider}-{self.new_id}"
        return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-").lower()


def compute_proposals(manifest: Manifest, polls: dict[str, PollResult]) -> list[Proposal]:
    """For each provider's current pins, find the newest same-family id the live endpoint
    advertises that is newer than the pin. One proposal per (provider, current pin) that has
    a strictly-newer live sibling.
    """
    proposals: list[Proposal] = []
    for provider in manifest.providers():
        poll = polls.get(provider)
        if poll is None or not poll.ok:
            continue
        for pin in manifest.by_provider(provider):
            newer = [mid for mid in poll.model_ids if is_newer(mid, pin.id)]
            if not newer:
                continue
            best = max(newer, key=version_tuple)
            proposals.append(
                Proposal(
                    provider=provider,
                    current_id=pin.id,
                    new_id=best,
                    inferred_capabilities=pin.capabilities,
                    note=(
                        "capabilities copied from the current pin and NOT verified against the "
                        "new model — confirm vision/context before merging (a point/turbo "
                        "release can regress)."
                    ),
                )
            )
    return proposals


# ── manifest rewriting (for the proposed PR diff) ───────────────────────────────────────
# One pattern for the bare and the quoted (`"`/`'`) forms: the optional quote is captured in
# groups 1 and 2 so the same quote style is preserved on the new id.
_ID_LINE_RE = re.compile(r'(\bid:\s*["\']?)%s(["\']?\s*$)', re.MULTILINE)


def rewrite_manifest_text(text: str, current_id: str, new_id: str) -> str:
    """Return `text` with the first `id: <current_id>` line repointed to `new_id`.

    A minimal, surgical edit — it only swaps the id in place so the proposal diff is small
    and reviewable. Quoted and bare forms are both handled. It does NOT touch capabilities
    (the human edits those if the new model differs) — surfacing the unverified-capabilities
    caveat in the proposal body is the safety mechanism, not a silent capability rewrite.

    `new_id` comes from a provider's endpoint, so it is inserted via a CALLABLE replacement
    (not a replacement STRING) — a `re.sub` replacement string would interpret `\\g<…>`/`\\1`
    backslash escapes in the id and corrupt the manifest. The callable inserts `new_id`
    verbatim.
    """
    pat = re.compile(_ID_LINE_RE.pattern % re.escape(current_id), re.MULTILINE)
    new_text, n = pat.subn(lambda m: f"{m.group(1)}{new_id}{m.group(2)}", text, count=1)
    return new_text if n else text


# ── proposing: gh PR, else a dated report ───────────────────────────────────────────────
def _gh_available() -> bool:
    import shutil

    if not shutil.which("gh"):
        return False
    try:
        res = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _proposal_branch(proposal: Proposal) -> str:
    return f"{PROPOSAL_MARKER}/{proposal.slug}"


def _open_pr_exists(proposal: Proposal) -> bool:
    """True iff an OPEN PR for this exact bump already exists (idempotency guard).

    Matches on the proposal branch name (head) — deterministic per (provider, new id), so a
    re-run that finds the same newer model won't open a duplicate PR.
    """
    branch = _proposal_branch(proposal)
    try:
        res = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--head", branch, "--json", "number"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return False
    return isinstance(data, list) and len(data) > 0


def _proposal_body(proposal: Proposal, poll: PollResult | None) -> str:
    caps = ", ".join(proposal.inferred_capabilities)
    advertised = ""
    if poll and proposal.new_id in poll.model_ids:
        advertised = f"\nThe provider's live model list advertises `{proposal.new_id}`."
    return (
        f"## Proposed model bump — `{proposal.provider}`\n\n"
        f"The daily model-freshness checker found a newer model for `{proposal.provider}`.\n\n"
        f"| | model id |\n|---|---|\n"
        f"| current pin | `{proposal.current_id}` |\n"
        f"| proposed | `{proposal.new_id}` |\n"
        f"{advertised}\n\n"
        f"**Capabilities (copied from the current pin, UNVERIFIED for the new model):** {caps}\n\n"
        f"> {proposal.note}\n\n"
        f"This is a SEMI-automatic proposal. A newer model can regress (a turbo/point release "
        f"may drop `vision` or shrink context), so this is **not** auto-merged — review the new "
        f"model's real capabilities, fix the `capabilities:`/`context:` if they changed, then "
        f"merge.\n"
    )


def propose_via_gh(
    proposal: Proposal, poll: PollResult | None, *, dry_run: bool, manifest_path: Path = MANIFEST_PATH
) -> str:
    """Open a PR repointing the manifest id. Returns a status string. Idempotent.

    Creates a fresh branch off HEAD, applies the surgical id swap to ``manifest_path``,
    commits, pushes, and opens a PR — unless an open PR for this exact bump already exists
    (then it's a no-op). ``manifest_path`` is threaded from ``run()`` so the file actually
    rewritten is the one the proposals were computed from (not a stale module global).
    """
    if _open_pr_exists(proposal):
        return f"skip: open PR already exists for {proposal.slug}"
    branch = _proposal_branch(proposal)
    title = f"chore(models): bump {proposal.provider} {proposal.current_id} -> {proposal.new_id} ({PROPOSAL_MARKER})"
    body = _proposal_body(proposal, poll)
    if dry_run:
        return f"dry-run: would open PR on branch {branch}: {title}"

    text = manifest_path.read_text(encoding="utf-8")
    new_text = rewrite_manifest_text(text, proposal.current_id, proposal.new_id)
    if new_text == text:
        return f"skip: could not locate id '{proposal.current_id}' in manifest to rewrite"

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT)
        )

    base = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    if _git("checkout", "-B", branch).returncode != 0:
        return f"error: could not create branch {branch}"
    try:
        manifest_path.write_text(new_text, encoding="utf-8")
        _git("add", str(manifest_path.relative_to(REPO_ROOT)))
        if _git("commit", "-m", title).returncode != 0:
            return f"error: commit failed for {proposal.slug}"
        if _git("push", "-u", "origin", branch, "--force-with-lease").returncode != 0:
            return f"error: push failed for {branch}"
        res = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
    finally:
        _git("checkout", base)
    if res.returncode != 0:
        return f"error: gh pr create failed for {proposal.slug}: {res.stderr.strip()}"
    return f"opened PR for {proposal.slug}: {res.stdout.strip()}"


def write_report(proposals: list[Proposal], polls: dict[str, PollResult]) -> Path:
    """Write a dated markdown report of all proposals (the gh-absent fallback).

    The path is dated per-run (one report per day); content is deterministic for a given set
    of proposals so a same-day re-run overwrites rather than accumulating noise.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"{today}-model-freshness.md"
    lines = [
        f"# Model-freshness report — {today}",
        "",
        "The daily checker ran. `gh` (or its auth) was unavailable, so proposed bumps are",
        "recorded here instead of opened as PRs. Apply a bump by editing",
        "`lib/contracts/models.yaml` after verifying the new model's real capabilities.",
        "",
    ]
    if not proposals:
        # Be honest about coverage: "all current" is only true if every provider was actually
        # polled. If some were skipped (no key) or errored, say so — a missing key must not
        # masquerade as "current".
        if all(pr.ok for pr in polls.values()):
            lines.append("**No newer models found.** Every polled provider pin is current.")
        else:
            ok = sum(1 for pr in polls.values() if pr.ok)
            lines.append(
                f"**No bumps from the {ok} provider(s) successfully polled.** "
                "Coverage is INCOMPLETE — some providers were skipped or errored (see below); "
                "this is NOT a clean bill of health for them."
            )
    else:
        lines.append("## Proposed bumps")
        lines.append("")
        for p in proposals:
            caps = ", ".join(p.inferred_capabilities)
            lines.append(f"- **{p.provider}**: `{p.current_id}` -> `{p.new_id}`")
            lines.append(f"  - capabilities (copied from current pin, UNVERIFIED): {caps}")
            lines.append(f"  - {p.note}")
    skipped = [pr for pr in polls.values() if not pr.ok]
    if skipped:
        lines.append("")
        lines.append("## Providers skipped / errored")
        lines.append("")
        for pr in skipped:
            reason = pr.skipped or pr.error or "unknown"
            lines.append(f"- **{pr.provider}**: {reason}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── top-level run ───────────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    proposals: list[Proposal]
    polls: dict[str, PollResult]
    actions: list[str] = field(default_factory=list)
    report_path: Path | None = None
    used_gh: bool = False


def run(
    *,
    manifest_path: Path = MANIFEST_PATH,
    timeout: float = 20.0,
    dry_run: bool = False,
    force_report: bool = False,
    pollers: dict[str, Callable[[float], PollResult]] | None = None,
    gh_available: Callable[[], bool] | None = None,
) -> RunResult:
    """Poll providers, compute proposals, and PROPOSE (PR via gh, else a dated report).

    Hooks (`pollers`, `gh_available`) are injected for tests — production passes None and
    the real implementations are used.
    """
    manifest = load_manifest(manifest_path)
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError("manifest invalid:\n  - " + "\n  - ".join(problems))

    pollers = pollers or POLLERS
    polls: dict[str, PollResult] = {}
    for provider in manifest.providers():
        poller = pollers.get(provider)
        polls[provider] = poller(timeout) if poller else PollResult(provider, False, skipped="no poller")

    proposals = compute_proposals(manifest, polls)
    result = RunResult(proposals=proposals, polls=polls)

    gh_ok = (gh_available or _gh_available)() and not force_report
    result.used_gh = gh_ok
    if gh_ok:
        for proposal in proposals:
            status = propose_via_gh(
                proposal, polls.get(proposal.provider), dry_run=dry_run, manifest_path=manifest_path
            )
            result.actions.append(status)
        if not proposals:
            result.actions.append("no newer models found")
    elif dry_run:
        # --dry-run must NEVER write (the CLI contract). In the gh-absent branch that means
        # NOT writing the dated report either — just record what would happen.
        result.actions.append(
            f"dry-run: would write a report with {len(proposals)} proposal(s) (no gh)"
        )
    else:
        path = write_report(proposals, polls)
        result.report_path = path
        result.actions.append(f"wrote report {path}")
    return result


def _print_human(result: RunResult) -> None:
    print(f"model-freshness: polled {len(result.polls)} provider(s)")
    for prov, poll in sorted(result.polls.items()):
        if poll.ok:
            print(f"  ✔ {prov}: {len(poll.model_ids)} models")
        else:
            print(f"  · {prov}: skipped ({poll.skipped or poll.error})")
    if result.proposals:
        print(f"\nproposed {len(result.proposals)} bump(s):")
        for p in result.proposals:
            print(f"  ▸ {p.provider}: {p.current_id} -> {p.new_id}  [{', '.join(p.inferred_capabilities)}]")
    elif all(pr.ok for pr in result.polls.values()):
        print("\nno newer models found — every polled provider pin is current")
    else:
        ok = sum(1 for pr in result.polls.values() if pr.ok)
        print(f"\nno bumps from the {ok} provider(s) polled — coverage INCOMPLETE "
              "(some providers skipped/errored; not a clean bill of health)")
    for action in result.actions:
        print(f"  {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model-freshness",
        description="Propose model-version bumps for lib/contracts/models.yaml (PROPOSE, never merge).",
    )
    parser.add_argument("--validate", action="store_true", help="validate the manifest and exit")
    parser.add_argument("--dry-run", action="store_true", help="poll + compute, but never open a PR / write a report")
    parser.add_argument("--report", action="store_true", help="force the dated-report path even if gh is available")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request HTTP timeout seconds")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary instead of human text")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest()
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.validate:
        problems = validate_manifest(manifest)
        if problems:
            print("manifest INVALID:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"manifest OK — {len(manifest.models)} models, {len(manifest.roles)} roles, "
              f"{len(manifest.aliases)} aliases")
        return 0

    try:
        result = run(dry_run=args.dry_run, force_report=args.report, timeout=args.timeout)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(
            {
                "proposals": [
                    {"provider": p.provider, "current": p.current_id, "new": p.new_id,
                     "capabilities": list(p.inferred_capabilities)}
                    for p in result.proposals
                ],
                "polls": {k: {"ok": v.ok, "count": len(v.model_ids), "skipped": v.skipped, "error": v.error}
                          for k, v in result.polls.items()},
                "actions": result.actions,
                "report": str(result.report_path) if result.report_path else None,
                "used_gh": result.used_gh,
            },
            indent=2,
        ))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
