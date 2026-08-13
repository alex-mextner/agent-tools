from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEFTHOOK = (ROOT / "git-hooks" / "lefthook.yml").read_text(encoding="utf-8")


def test_direct_oxc_gates_are_active_not_commented_out():
    assert "    oxlint:\n" in LEFTHOOK
    assert "    oxfmt:\n" in LEFTHOOK
    assert "# oxlint:" not in LEFTHOOK
    assert "# oxfmt:" not in LEFTHOOK


def test_direct_oxc_gate_distinguishes_undeclared_from_unavailable():
    # Adoption is based on tracked package manifests, so monorepo/workspace declarations count.
    assert 'git",["ls-files","*package.json"]' in LEFTHOOK
    assert "Oxlint is not declared in tracked package manifests" in LEFTHOOK
    assert "Oxfmt is not declared in tracked package manifests" in LEFTHOOK

    # Once declared, inability to execute is an error rather than the same successful skip path.
    assert "Oxlint is declared in this repository but cannot be executed" in LEFTHOOK
    assert "Oxfmt is declared in this repository but cannot be executed" in LEFTHOOK
    assert LEFTHOOK.count("exit 127") >= 4


def test_direct_oxc_gate_supports_common_repository_package_managers_without_installing():
    assert "pnpm exec oxlint" in LEFTHOOK
    assert "pnpm exec oxfmt" in LEFTHOOK
    assert "yarn oxlint" in LEFTHOOK
    assert "yarn oxfmt" in LEFTHOOK
    assert "npx --no-install oxlint" in LEFTHOOK
    assert "npx --no-install oxfmt" in LEFTHOOK
