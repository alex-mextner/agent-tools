"""Tests for the generalized two-layer config-cascade loader (``agenttools_config``).

Run from the repo root::

    uv run --with pytest --with pyyaml pytest tests/test_agenttools_config.py -q

These cover the tool-agnostic cascade extracted from rig-cli's ``riglib.config``:
global-only, repo-only, both (repo wins), deep merge of nested dicts, list replacement,
``$XDG_CONFIG_HOME`` override, missing-file handling, and a ``schema_validate`` hook that
raises being surfaced. Every test builds a real ``$XDG_CONFIG_HOME`` and a real repo dir
on disk and parses real YAML — no mocks of the thing under test.

The loader's own invariant is that PyYAML is needed only to actually LOAD a file, so the
suite skips (it does not fail) when PyYAML is absent — mirroring
``tests/test_model_freshness.py``. The README documents the ``--with pyyaml`` invocation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# These tests parse real YAML, so PyYAML must be importable. Skip cleanly (don't fail) when
# it isn't — same convention as test_model_freshness.py. The package itself imports without
# PyYAML (lazy import); only loading a config file needs it.
pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_config import (  # noqa: E402
    ConfigError,
    LoadedConfig,
    deep_merge,
    global_config_path,
    load_config,
    repo_config_path,
)

TOOL = "rig"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated XDG home + an empty repo dir, with ``$XDG_CONFIG_HOME`` pointed at it.

    Returns a small helper object: ``env.repo`` (the repo root), ``env.write_global(text)``
    and ``env.write_repo(text)`` to drop the two layer files.
    """
    xdg = tmp_path / "xdg"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    class _Env:
        def __init__(self):
            self.repo = repo

        def write_global(self, text: str) -> Path:
            path = global_config_path(TOOL)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return path

        def write_repo(self, text: str) -> Path:
            path = repo_config_path(TOOL, repo)
            path.write_text(text, encoding="utf-8")
            return path

    return _Env()


# --------------------------------------------------------------------------- paths


def test_global_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert global_config_path("rig") == tmp_path / "cfg" / "rig" / "config.yaml"


def test_global_path_falls_back_to_home_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = global_config_path("rig")
    assert path == Path("~/.config").expanduser() / "rig" / "config.yaml"


def test_global_path_empty_xdg_falls_back(monkeypatch):
    # An empty (not just unset) XDG_CONFIG_HOME must fall back, not resolve to "/rig/...".
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    path = global_config_path("rig")
    assert path == Path("~/.config").expanduser() / "rig" / "config.yaml"


def test_repo_path_is_tool_dot_yaml(tmp_path):
    assert repo_config_path("rig", tmp_path) == tmp_path / "rig.yaml"


# --------------------------------------------------------------------------- cascade


def test_no_layers_yields_empty(env):
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert isinstance(loaded, LoadedConfig)
    assert loaded.data == {}
    assert loaded.layers == []
    assert loaded.global_path is None
    assert loaded.repo_path is None
    assert loaded.is_empty is True


def test_global_only(env):
    gpath = env.write_global("version: 1\nname: from-global\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert loaded.data == {"version": 1, "name": "from-global"}
    assert loaded.global_path == gpath
    assert loaded.repo_path is None
    assert loaded.layers == [f"global:{gpath}"]
    assert loaded.is_empty is False


def test_repo_only(env):
    rpath = env.write_repo("version: 1\nname: from-repo\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert loaded.data == {"version": 1, "name": "from-repo"}
    assert loaded.global_path is None
    assert loaded.repo_path == rpath
    assert loaded.layers == [f"repo:{rpath}"]


def test_both_repo_wins_on_scalar(env):
    env.write_global("name: from-global\nshared: g\nonly_global: 1\n")
    env.write_repo("name: from-repo\nshared: r\nonly_repo: 2\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    # repo overrides shared scalars; each layer's unique keys survive.
    assert loaded.data == {
        "name": "from-repo",
        "shared": "r",
        "only_global": 1,
        "only_repo": 2,
    }
    assert len(loaded.layers) == 2
    assert loaded.layers[0].startswith("global:")
    assert loaded.layers[1].startswith("repo:")


def test_deep_merge_of_nested_dicts(env):
    env.write_global("a:\n  x: 1\n  y: 2\n  nested:\n    deep: keep\n")
    env.write_repo("a:\n  y: 99\n  z: 3\n  nested:\n    extra: add\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    # Nested dicts merge recursively: global a.x and a.nested.deep survive; repo wins on a.y.
    assert loaded.data == {
        "a": {
            "x": 1,
            "y": 99,
            "z": 3,
            "nested": {"deep": "keep", "extra": "add"},
        }
    }


def test_lists_replace_wholesale(env):
    env.write_global("items:\n  - one\n  - two\n  - three\n")
    env.write_repo("items:\n  - only\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    # A list in the repo file fully REPLACES the global list (atomic, never appended).
    assert loaded.data == {"items": ["only"]}


def test_xdg_override_relocates_global_layer(tmp_path, monkeypatch):
    # Two different XDG homes resolve to two different global files; the active one wins.
    repo = tmp_path / "repo"
    repo.mkdir()
    home_a = tmp_path / "a"
    (home_a / TOOL).mkdir(parents=True)
    (home_a / TOOL / "config.yaml").write_text("name: A\n", encoding="utf-8")
    home_b = tmp_path / "b"
    (home_b / TOOL).mkdir(parents=True)
    (home_b / TOOL / "config.yaml").write_text("name: B\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_a))
    assert load_config(tool=TOOL, repo_root=repo).data == {"name": "A"}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_b))
    assert load_config(tool=TOOL, repo_root=repo).data == {"name": "B"}


def test_include_global_false_skips_global(env):
    env.write_global("name: from-global\n")
    env.write_repo("other: 1\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo, include_global=False)
    assert loaded.data == {"other": 1}
    assert loaded.global_path is None
    assert all(not layer.startswith("global:") for layer in loaded.layers)


def test_include_repo_false_loads_global_only(env):
    # The documented "global only" path: ignore the per-repo file EVEN WHEN IT EXISTS.
    env.write_global("name: from-global\nkept: g\n")
    env.write_repo("name: SHOULD-NOT-BE-USED\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo, include_repo=False)
    assert loaded.data == {"name": "from-global", "kept": "g"}
    assert loaded.repo_path is None
    assert all(not layer.startswith("repo:") for layer in loaded.layers)


def test_include_repo_false_with_no_global_is_empty(env):
    env.write_repo("name: ignored\n")
    loaded = load_config(
        tool=TOOL, repo_root=env.repo, include_global=False, include_repo=False
    )
    assert loaded.data == {}
    assert loaded.is_empty is True


def test_explicit_config_overrides_include_repo_false(env, tmp_path):
    # An explicitly named file is always loaded, even with include_repo=False.
    env.write_global("kept: g\n")
    override = tmp_path / "override.yaml"
    override.write_text("name: from-explicit\n", encoding="utf-8")
    loaded = load_config(
        tool=TOOL, repo_root=env.repo, explicit_config=override, include_repo=False
    )
    assert loaded.data == {"name": "from-explicit", "kept": "g"}
    assert loaded.repo_path == override.resolve()


def test_global_path_is_absolute_under_relative_xdg(tmp_path, monkeypatch):
    # Even with a RELATIVE XDG_CONFIG_HOME, the recorded global_path is absolute.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relcfg" / TOOL).mkdir(parents=True)
    (tmp_path / "relcfg" / TOOL / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relcfg")  # relative on purpose
    loaded = load_config(tool=TOOL, repo_root=repo)
    assert loaded.data == {"a": 1}
    assert loaded.global_path is not None
    assert loaded.global_path.is_absolute()


def test_explicit_config_replaces_repo_layer(env, tmp_path):
    env.write_global("name: from-global\nkept: g\n")
    env.write_repo("name: SHOULD-NOT-BE-USED\n")  # ignored when explicit_config is given
    override = tmp_path / "override.yaml"
    override.write_text("name: from-explicit\n", encoding="utf-8")
    loaded = load_config(tool=TOOL, repo_root=env.repo, explicit_config=override)
    assert loaded.data == {"name": "from-explicit", "kept": "g"}
    assert loaded.repo_path == override.resolve()
    assert any(layer.startswith("config:") for layer in loaded.layers)


def test_explicit_config_missing_raises(env, tmp_path):
    with pytest.raises(ConfigError, match="--config file not found"):
        load_config(
            tool=TOOL, repo_root=env.repo, explicit_config=tmp_path / "nope.yaml"
        )


# --------------------------------------------------- missing-file / malformed handling


def test_missing_files_are_not_errors(env):
    # Neither layer exists; loading is fine (yields {}), not an error.
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert loaded.data == {}


def test_empty_yaml_file_is_empty_dict(env):
    env.write_repo("")  # an empty file parses to None → {}
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert loaded.data == {}
    # ...but the file WAS present, so it is recorded as a layer.
    assert loaded.repo_path is not None


def test_non_mapping_root_raises(env):
    env.write_repo("- just\n- a\n- list\n")  # a top-level list, not a mapping
    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        load_config(tool=TOOL, repo_root=env.repo)


def test_invalid_yaml_raises_configerror_not_traceback(env):
    env.write_repo("key: : bad: yaml\n")  # broken YAML
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(tool=TOOL, repo_root=env.repo)


# ----------------------------------------------------------------- schema_validate hook


def test_validate_hook_receives_merged_dict(env):
    env.write_global("a: 1\n")
    env.write_repo("b: 2\n")
    seen = {}

    def validate(data: dict) -> None:
        seen.update(data)

    load_config(tool=TOOL, repo_root=env.repo, schema_validate=validate)
    assert seen == {"a": 1, "b": 2}


def test_validate_hook_that_raises_is_surfaced(env):
    env.write_repo("version: not-an-int\n")

    def validate(data: dict) -> None:
        if not isinstance(data.get("version"), int):
            raise ConfigError("version must be an int")

    with pytest.raises(ConfigError, match="version must be an int"):
        load_config(tool=TOOL, repo_root=env.repo, schema_validate=validate)


def test_validate_hook_arbitrary_exception_propagates_unchanged(env):
    env.write_repo("x: 1\n")

    class Boom(RuntimeError):
        pass

    def validate(data: dict) -> None:
        raise Boom("custom")

    # A non-ConfigError exception from the hook is NOT swallowed or re-wrapped.
    with pytest.raises(Boom, match="custom"):
        load_config(tool=TOOL, repo_root=env.repo, schema_validate=validate)


def test_no_validate_hook_means_no_schema_check(env):
    # Without a hook, ANY mapping is accepted — the loader owns no domain schema.
    env.write_repo("totally: {arbitrary: keys}\nversion: not-an-int\n")
    loaded = load_config(tool=TOOL, repo_root=env.repo)
    assert loaded.data == {"totally": {"arbitrary": "keys"}, "version": "not-an-int"}


# --------------------------------------------------------------------- deep_merge unit


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1}, "list": [1, 2]}
    over = {"a": {"y": 2}, "list": [9]}
    out = deep_merge(base, over)
    assert out == {"a": {"x": 1, "y": 2}, "list": [9]}
    # inputs untouched
    assert base == {"a": {"x": 1}, "list": [1, 2]}
    assert over == {"a": {"y": 2}, "list": [9]}


def test_deep_merge_scalar_over_dict_replaces():
    # If over makes a key a scalar where base had a dict, the scalar wins (no merge).
    out = deep_merge({"a": {"x": 1}}, {"a": 5})
    assert out == {"a": 5}


def test_deep_merge_dict_over_scalar_replaces():
    out = deep_merge({"a": 5}, {"a": {"x": 1}})
    assert out == {"a": {"x": 1}}


def test_deep_merge_calling_does_not_mutate_then_documents_leaf_aliasing():
    # Calling deep_merge never mutates the inputs (the contract for the load path)...
    base = {"only_base": [1, 2]}
    over = {"only_over": [9]}
    out = deep_merge(base, over)
    assert base == {"only_base": [1, 2]} and over == {"only_over": [9]}
    # ...but a NON-merged leaf container is stored by reference (the documented caveat:
    # mutating it inside the result reaches back into the input). load_config never does
    # this (each layer's parsed YAML is local + discarded), but pin the behavior so the
    # docstring's "deepcopy the result if you need isolation" note stays accurate.
    assert out["only_base"] is base["only_base"]
    assert out["only_over"] is over["only_over"]
