"""The shell command-splitting parser is duplicated across hooks — keep the copies identical.

`visual-proof-gate` and `skills-read-gate` are standalone scripts run as their own subprocess
with no shared import path, so `_scan_line` / `_split_unquoted_lines` / `_shell_tokens` are
copied into both by design (see the SYNC comments in each file).

That convention has already cost this repo a real bug: `is_skip_commit`'s first-segment-only
flaw (agent-tools#174) shipped precisely because an earlier fix was applied to only one twin,
and the SYNC comment — being a comment — could not notice. agent-tools#472 then found the same
shape again: both copies were newline-blind, so a multi-line `git commit` bypassed BOTH gates.

This test makes the comment enforceable. It compares the parser functions structurally (the
AST with docstrings stripped), so the copies may keep their own prose — each docstring cites
its own hook's consumers — but the CODE must not drift. A fix applied to one copy and not the
other fails here instead of silently leaving one gate open.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "agent-hooks"
_SOURCES = {
    "visual-proof-gate": _HOOKS / "visual-proof-gate" / "visual_proof_gate.py",
    "skills-read-gate": _HOOKS / "skills-read-gate" / "skills_read_gate.py",
}

# The parser functions duplicated verbatim across both hooks.
_SHARED_FUNCTIONS = ("_scan_line", "_split_unquoted_lines", "_shell_tokens")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalized_source(path: Path, func_name: str) -> str:
    """The function's AST dumped with its docstring removed, so prose may differ but code
    may not. Comments are absent from the AST already."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            stripped = ast.FunctionDef(
                name=node.name, args=node.args, body=body,
                decorator_list=[], returns=node.returns, type_params=[],
            )
            return ast.dump(ast.fix_missing_locations(stripped))
    raise AssertionError(f"{func_name} not found in {path}")


@pytest.mark.parametrize("func_name", _SHARED_FUNCTIONS)
def test_parser_copies_are_structurally_identical(func_name):
    """Every duplicated parser function must have identical code in both hooks."""
    dumps = {hook: _normalized_source(path, func_name) for hook, path in _SOURCES.items()}
    assert len(set(dumps.values())) == 1, (
        f"{func_name} has drifted between the hooks — apply the change to BOTH copies "
        f"({', '.join(str(p) for p in _SOURCES.values())})"
    )


@pytest.mark.parametrize("command", [
    "git commit -m x",
    "cd /repo\ngit commit -m x",
    "set -e\nnpm test",
    'echo "prose about git\nand commit"',
    "cat > f <<'EOF'\ngit commit -m x\nEOF",
    "grep x <<< 'note'\ngit commit -m x",
    "git add -A &&\ngit commit -m x",
    "git \\\n  commit -m x",
    "echo x\rgit commit -m y",
    "# a comment line\ngit commit -m x",
    'git commit -m "title\n\nbody"',
    "cd /repo\r\ngit commit -m x",
])
def test_both_hooks_tokenize_a_command_identically(command):
    """Structural equality is checked above; this checks the copies BEHAVE the same on the
    shapes that motivated agent-tools#472, so a future divergence is caught even if someone
    reformats one copy past the AST comparison."""
    vpg = _load("visual-proof-gate", _SOURCES["visual-proof-gate"])
    srg = _load("skills-read-gate", _SOURCES["skills-read-gate"])
    assert vpg._shell_tokens(command) == srg._shell_tokens(command)
    assert vpg._split_unquoted_lines(command) == srg._split_unquoted_lines(command)
