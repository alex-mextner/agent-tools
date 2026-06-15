#!/usr/bin/env python3
"""Unit tests for require_ticket_before_commit (stdlib unittest, no deps).

Run::
    python3 test_require_ticket.py            # this directory
    python3 -m pytest test_require_ticket.py  # also works under pytest
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE / "require_ticket_before_commit.py"

# Import the script as a module so we can unit-test its helpers directly.
_spec = importlib.util.spec_from_file_location("require_ticket", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def run_hook(
    command: str,
    *,
    strict: bool = False,
    env_extra: dict | None = None,
    cwd: str | None = None,
) -> tuple[int, str | None]:
    """Run the script end-to-end with a JSON event on stdin; return (exit, decision).

    The event's `cwd` defaults to an isolated empty temp dir (no git repo) so the
    hook's branch detection returns "" — otherwise the host checkout's branch name
    could leak a ticket pattern and make these tests non-deterministic.
    """
    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    if strict:
        env["REQUIRE_TICKET_STRICT"] = "1"
    if env_extra:
        env.update(env_extra)

    with tempfile.TemporaryDirectory() as isolated:
        event_cwd = cwd or isolated
        event = json.dumps({"args": {"command": command}, "cwd": event_cwd})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,  # process cwd also outside any git repo
            timeout=10,
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"empty stdout; stderr={proc.stderr!r}")
    decision = json.loads(out)["decision"]
    return proc.returncode, decision


class TicketHeuristic(unittest.TestCase):
    def test_detects_github_issue(self):
        self.assertTrue(mod.has_ticket_reference("feat: export (Refs #123)"))

    def test_detects_qualified_github(self):
        self.assertTrue(mod.has_ticket_reference("feat: x alex-mextner/task-cli#4"))

    def test_detects_linear_key(self):
        self.assertTrue(mod.has_ticket_reference("fix: crash ENG-456"))

    def test_detects_jira_style_lowercase_subject(self):
        self.assertTrue(mod.has_ticket_reference("ABC-12 add export"))

    def test_detects_task_cli_forms(self):
        self.assertTrue(mod.has_ticket_reference("done task:ABC-12"))
        self.assertTrue(mod.has_ticket_reference("done task #7"))
        self.assertTrue(mod.has_ticket_reference("done T-9"))

    def test_detects_trailer_keywords(self):
        self.assertTrue(mod.has_ticket_reference("Closes #12"))
        self.assertTrue(mod.has_ticket_reference("Fixes ABC-3"))

    def test_detects_tracker_url(self):
        self.assertTrue(
            mod.has_ticket_reference("see https://github.com/o/r/issues/42")
        )
        self.assertTrue(
            mod.has_ticket_reference("see https://linear.app/acme/issue/ENG-7/title")
        )

    def test_no_false_positive_on_plain_subject(self):
        self.assertFalse(mod.has_ticket_reference("feat: add CSV export to reports"))

    def test_no_false_positive_on_version_or_date(self):
        # A bare version / date must not look like a KEY-NUM ticket.
        self.assertFalse(mod.has_ticket_reference("chore: bump to v2 on 2026-06-15"))

    def test_single_letter_key_is_not_a_ticket(self):
        # KEY-NUM needs >=2 uppercase letters; `A1-456` must NOT count.
        self.assertFalse(mod.has_ticket_reference("perf: shrink A1-456 buffer"))

    def test_bare_fix_keyword_is_not_a_reference(self):
        # A trailer keyword must point at a real ref — `fix: null deref` is not one.
        self.assertFalse(mod.has_ticket_reference("fix: null deref in handler"))
        self.assertFalse(mod.has_ticket_reference("Fixes the broken parser"))

    def test_fix_keyword_with_real_ref_is_a_reference(self):
        self.assertTrue(mod.has_ticket_reference("Fixes #42"))
        self.assertTrue(mod.has_ticket_reference("Fixes ENG-3"))


class Exemptions(unittest.TestCase):
    def test_chore_type_is_exempt(self):
        self.assertTrue(mod.is_exempt("chore: bump lockfile"))

    def test_docs_type_is_exempt(self):
        self.assertTrue(mod.is_exempt("docs(readme): fix typo"))

    def test_wip_is_exempt(self):
        self.assertTrue(mod.is_exempt("wip: spike"))

    def test_fixup_is_exempt(self):
        self.assertTrue(mod.is_exempt("fixup! feat: thing"))

    def test_merge_is_exempt(self):
        self.assertTrue(mod.is_exempt("Merge branch 'main' into x"))

    def test_feat_is_not_exempt(self):
        self.assertFalse(mod.is_exempt("feat: add export"))

    def test_fix_is_not_exempt(self):
        self.assertFalse(mod.is_exempt("fix: null deref"))

    def test_body_line_does_not_exempt(self):
        # An exempt-looking type word in the body must not exempt a real `feat:`.
        self.assertFalse(mod.is_exempt("feat: add export\n\ndocs: also touched a doc"))


class MessageExtraction(unittest.TestCase):
    def test_pulls_dash_m(self):
        msg = mod.commit_message_from_command('git commit -m "feat: x Refs #5"')
        self.assertIn("Refs #5", msg)

    def test_pulls_attached_dash_m(self):
        msg = mod.commit_message_from_command('git commit -m"ENG-1 thing"')
        self.assertIn("ENG-1", msg)

    def test_pulls_message_equals(self):
        msg = mod.commit_message_from_command('git commit --message="task:T-3 thing"')
        self.assertIn("task:T-3", msg)

    def test_pulls_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: big change\n\nCloses #99\n")
            path = fh.name
        try:
            msg = mod.commit_message_from_command(f"git commit -F {path}")
            self.assertIn("Closes #99", msg)
        finally:
            os.unlink(path)

    def test_unbalanced_quotes_fall_back_to_raw(self):
        # shlex would raise; we must not crash, just scan the raw string.
        msg = mod.commit_message_from_command('git commit -m "unterminated #7')
        self.assertIn("#7", msg)


class EndToEnd(unittest.TestCase):
    def test_missing_ticket_warns_but_allows_by_default(self):
        code, decision = run_hook('git commit -m "feat: add export"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_present_ticket_allows_clean(self):
        code, decision = run_hook('git commit -m "feat: add export (Refs #123)"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_missing_ticket_blocks_in_strict_mode(self):
        code, decision = run_hook('git commit -m "feat: add export"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_ticket_present_allows_even_in_strict_mode(self):
        code, decision = run_hook(
            'git commit -m "feat: add export ENG-9"', strict=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_chore_exempt_in_strict_mode(self):
        code, decision = run_hook('git commit -m "chore: bump dep"', strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_amend_not_gated_in_strict_mode(self):
        code, decision = run_hook("git commit --amend --no-edit", strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_non_commit_command_allowed(self):
        code, decision = run_hook("git status", strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_custom_exempt_types_in_strict_mode(self):
        code, decision = run_hook(
            'git commit -m "perf: faster export"',
            strict=True,
            env_extra={"REQUIRE_TICKET_EXEMPT_TYPES": "perf"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_bare_fix_blocks_in_strict_mode(self):
        # `fix:` is a bugfix (not exempt) and `null deref` is not a ticket ref.
        code, decision = run_hook('git commit -m "fix: null deref"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_relative_message_file_resolves_against_cwd(self):
        # -F with a RELATIVE path must resolve against the event cwd, not the hook's.
        with tempfile.TemporaryDirectory() as repo:
            msg_path = Path(repo) / "COMMIT_MSG.txt"
            msg_path.write_text("feat: add export\n\nCloses #321\n", encoding="utf-8")
            code, decision = run_hook(
                "git commit -F COMMIT_MSG.txt", strict=True, cwd=repo
            )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
