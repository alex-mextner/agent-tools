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
    strict: bool | None = None,
    env_extra: dict | None = None,
    cwd: str | None = None,
) -> tuple[int, str | None]:
    """Run the script end-to-end with a JSON event on stdin; return (exit, decision).

    The event's `cwd` defaults to an isolated empty temp dir (no git repo) so the
    hook's branch detection returns "" — otherwise the host checkout's branch name
    could leak a ticket pattern and make these tests non-deterministic.

    `strict`: None → leave REQUIRE_TICKET_STRICT UNSET (exercise the real default,
    which is now strict-block); True → `=1`; False → `=0` (explicit warn-only opt-out).
    """
    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_SKIP", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    if strict is True:
        env["REQUIRE_TICKET_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TICKET_STRICT"] = "0"
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


def _run_hook_raw(command: str, **kwargs) -> dict:
    """Like run_hook but also returns the protocol `message` (for marker assertions)."""
    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_SKIP", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    if kwargs.get("env_extra"):
        env.update(kwargs["env_extra"])
    with tempfile.TemporaryDirectory() as isolated:
        event = json.dumps({"args": {"command": command}, "cwd": isolated})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,
            timeout=10,
        )
    payload = json.loads((proc.stdout or "").strip())
    return {
        "code": proc.returncode,
        "decision": payload["decision"],
        "message": payload.get("message", ""),
    }


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
    def test_missing_ticket_blocks_by_default(self):
        # The new default (REQUIRE_TICKET_STRICT unset) is a hard BLOCK.
        code, decision = run_hook('git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_block_message_carries_stable_marker(self):
        # The Docker test asserts on this fixed marker — it must lead the block message.
        proc_out = _run_hook_raw('git commit -m "feat: add export"')
        self.assertEqual(proc_out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(proc_out["decision"], "block")
        self.assertIn(mod.BLOCK_MARKER, proc_out["message"])
        self.assertIn("[require-ticket] BLOCKED: no ticket reference", proc_out["message"])

    def test_missing_ticket_warns_but_allows_when_strict_disabled(self):
        # REQUIRE_TICKET_STRICT=0 is the explicit warn-only opt-out.
        code, decision = run_hook('git commit -m "feat: add export"', strict=False)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_warn_mode_message_omits_block_marker(self):
        # An ALLOW (warn-only) must NOT carry BLOCK_MARKER — else an external check that greps
        # stdout for the marker gets a false positive on an allowed commit.
        out = _run_hook_raw('git commit -m "feat: add export"', env_extra={"REQUIRE_TICKET_STRICT": "0"})
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])

    def test_present_ticket_allows_clean(self):
        code, decision = run_hook('git commit -m "feat: add export (Refs #123)"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_missing_ticket_blocks_in_explicit_strict_mode(self):
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


class ValidRefFormsPassByDefault(unittest.TestCase):
    """Every accepted ticket-reference form must PASS end-to-end under the strict default
    (no REQUIRE_TICKET_STRICT set) — so a legitimately-ticketed commit is never wedged."""

    def _assert_allows(self, subject: str):
        code, decision = run_hook(f'git commit -m "{subject}"')
        self.assertEqual(code, 0, f"{subject!r} should pass but exit={code}")
        self.assertEqual(decision, "allow", f"{subject!r} should pass")

    def test_closes_hash(self):
        self._assert_allows("feat: add export\\n\\nCloses #123")

    def test_fixes_hash(self):
        self._assert_allows("fix: crash\\n\\nFixes #4")

    def test_bare_hash(self):
        self._assert_allows("feat: add export #88")

    def test_task_cli_key_num_id(self):
        self._assert_allows("feat: add export ENG-456")

    def test_task_cli_task_form(self):
        self._assert_allows("feat: add export task:ABC-12")

    def test_ticket_trailer(self):
        self._assert_allows("feat: add export [ticket: PROJ-9]")

    def test_ticket_trailer_with_slug(self):
        # a non-numeric task-cli id inside a [ticket: …] trailer also counts
        self._assert_allows("feat: add export [ticket: backfill-orders]")


class PerCommitEscapes(unittest.TestCase):
    """The two deliberate escapes must let a ticketless commit through under the strict default."""

    def test_skip_ticket_trailer_allows(self):
        code, decision = run_hook(
            'git commit -m "feat: x [skip-ticket: one-off backfill]"'
        )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_skip_ticket_trailer_requires_reason(self):
        # a blank `[skip-ticket:]` is NOT a valid escape — it must still block.
        code, decision = run_hook('git commit -m "feat: x [skip-ticket: ]"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_inline_skip_env_allows(self):
        code, decision = run_hook('REQUIRE_TICKET_SKIP=1 git commit -m "feat: x"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_inline_skip_falsey_does_not_allow(self):
        code, decision = run_hook('REQUIRE_TICKET_SKIP=0 git commit -m "feat: x"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_inline_skip_on_sibling_does_not_bypass(self):
        # the assignment is on a SIBLING command, not the `git commit` segment → still blocks.
        code, decision = run_hook(
            'REQUIRE_TICKET_SKIP=1 echo hi && git commit -m "feat: x"'
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_skip_trailer_helper(self):
        self.assertTrue(mod.has_skip_trailer("feat: x [skip-ticket: reason]"))
        self.assertFalse(mod.has_skip_trailer("feat: x"))
        self.assertFalse(mod.has_skip_trailer("feat: x [skip-ticket: ]"))

    def test_inline_skip_helper(self):
        self.assertTrue(mod.has_inline_skip('REQUIRE_TICKET_SKIP=1 git commit -m x'))
        self.assertTrue(mod.has_inline_skip('REQUIRE_TICKET_SKIP=yes git commit -m x'))
        self.assertFalse(mod.has_inline_skip('REQUIRE_TICKET_SKIP=0 git commit -m x'))
        self.assertFalse(mod.has_inline_skip('git commit -m x'))
        self.assertFalse(
            mod.has_inline_skip('REQUIRE_TICKET_SKIP=1 echo x; git commit -m y')
        )

    def test_inline_skip_with_shell_op_in_message_fails_safe(self):
        # The inline-skip parser splits on `;`/`&&`/`||` BEFORE quote-aware tokenizing, so a
        # commit MESSAGE that itself contains a shell operator breaks the segment and the inline
        # escape is silently NOT honored. Direction is fail-SAFE (the commit is gated, not
        # bypassed) — documented here so the behavior is intentional, not a silent surprise. The
        # robust escape for such a message is the `[skip-ticket: …]` trailer, which is unaffected.
        code, decision = run_hook(
            'REQUIRE_TICKET_SKIP=1 git commit -m "fix: handle a; b edge case"'
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class StrictToggle(unittest.TestCase):
    def test_strict_default_when_unset(self):
        # the module-level STRICT reflects the env at import; assert the falsey set directly.
        self.assertIn("0", mod._STRICT_FALSEY)
        self.assertIn("false", mod._STRICT_FALSEY)
        self.assertIn("off", mod._STRICT_FALSEY)

    def test_strict_zero_warns(self):
        code, decision = run_hook('git commit -m "feat: x"', strict=False)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_strict_unset_blocks(self):
        code, decision = run_hook('git commit -m "feat: x"', strict=None)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class GitDashCAndSkipScope(unittest.TestCase):
    def test_effective_cwd_honors_dash_C(self):
        # `git -C <repo> commit` acts on <repo> — branch detection must read THAT repo.
        self.assertEqual(mod.effective_cwd("git -C /srv/repo commit -m x", "/event"), "/srv/repo")
        self.assertEqual(mod.effective_cwd("git -C/srv/repo commit -m x", "/event"), "/srv/repo")
        # a relative -C resolves against the event cwd
        self.assertEqual(mod.effective_cwd("git -C sub commit -m x", "/event"), "/event/sub")
        # no -C → the event cwd is unchanged
        self.assertEqual(mod.effective_cwd("git commit -m x", "/event"), "/event")

    def test_argv_without_message_strips_message_values(self):
        # a flag named only in the MESSAGE is dropped; a real argv flag is kept
        self.assertNotIn("--amend", mod._argv_without_message('git commit -m "uses --amend"'))
        self.assertIn("--amend", mod._argv_without_message('git commit --amend -m "x"'))

    def test_skip_flag_in_message_does_not_exempt(self):
        # `--amend` inside the commit MESSAGE must NOT exempt a real, ticketless commit.
        code, decision = run_hook('git commit -m "support the --amend flag"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


if __name__ == "__main__":
    unittest.main(verbosity=2)
