"""
tests/test_tools.py
--------------------
Unit tests for tools.py's functions in isolation - i.e. calling
resolve_within_root(), handle_bash(), and handle_text_editor() (and its
private helpers) directly with plain Python arguments, no subprocess, no
API key, no live model.

This is a different kind of coverage than evals/run_evals.py:

    evals/run_evals.py  - end to end. Spawns `python main.py` as a real
                           subprocess, sends it real prompts, and checks
                           the *filesystem state* Claude leaves behind
                           after a real model picks tools and drives the
                           loop. Slow-ish, costs real API tokens, and
                           exercises main.py + tools.py + the model
                           together. Answers "does the whole agent work?"

    tests/test_tools.py - unit. Calls the functions in tools.py directly
                           with hand-picked inputs (including adversarial
                           ones like path traversal) and checks their
                           return values. Fast, free, no network. Answers
                           "does this one function do the right thing for
                           this one input?" - including edge cases that
                           would be slow, flaky, or hard to force a real
                           model into producing on purpose (a path escaping
                           root, a str_replace with zero or multiple
                           matches, a bash timeout, a declined command).

    Neither replaces the other: this suite would happily pass even if
    main.py's loop never called these functions correctly, and run_evals.py
    would rarely exercise the failure branches below anyway (a well-behaved
    model doesn't usually try to escape the project root).

Run with:  python -m unittest discover -s tests
       or: python -m unittest tests.test_tools
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools


class ResolveWithinRootTests(unittest.TestCase):
    """resolve_within_root() is the security boundary every file operation
    goes through - these are the cases that actually matter."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name).resolve()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_relative_path_resolves_under_root(self):
        result = tools.resolve_within_root(self.root, "notes.txt")
        self.assertEqual(result, self.root / "notes.txt")

    def test_nested_relative_path_resolves_under_root(self):
        result = tools.resolve_within_root(self.root, "a/b/c.txt")
        self.assertEqual(result, self.root / "a" / "b" / "c.txt")

    def test_dotdot_that_normalizes_back_inside_root_is_allowed(self):
        # "a/../b.txt" normalizes to "b.txt" - still inside root, so this
        # is legitimate and shouldn't be rejected just for containing "..".
        result = tools.resolve_within_root(self.root, "a/../b.txt")
        self.assertEqual(result, self.root / "b.txt")

    def test_dotdot_traversal_outside_root_is_rejected(self):
        with self.assertRaises(ValueError):
            tools.resolve_within_root(self.root, "../outside.txt")

    def test_absolute_path_outside_root_is_rejected(self):
        with self.assertRaises(ValueError):
            tools.resolve_within_root(self.root, "/etc/passwd")

    def test_absolute_path_inside_root_is_allowed(self):
        result = tools.resolve_within_root(self.root, str(self.root / "x.txt"))
        self.assertEqual(result, self.root / "x.txt")

    def test_symlink_escaping_root_is_rejected(self):
        # A symlink physically inside root that points outside it should
        # still be caught - resolve(strict=False) follows symlinks, so the
        # is_relative_to() check downstream is what has to catch this.
        outside_dir = tempfile.TemporaryDirectory()
        try:
            outside_target = Path(outside_dir.name) / "secret.txt"
            outside_target.write_text("shh")
            link = self.root / "escape"
            link.symlink_to(outside_target)
            with self.assertRaises(ValueError):
                tools.resolve_within_root(self.root, "escape")
        finally:
            outside_dir.cleanup()


class TextEditorTests(unittest.TestCase):
    """handle_text_editor() and the _view/_create/_str_replace/_insert
    helpers it dispatches to.

    Every mutating command (create/str_replace/insert) now goes through an
    approval gate the same shape as handle_bash()'s - see
    ApprovalGateTests below for that gate's own behavior. Here,
    AUTO_APPROVE_EDITS is patched on for the whole class so these tests can
    focus on what each command actually does to the filesystem, the same
    way BashTests patches AUTO_APPROVE_BASH on for its "does the command
    run correctly" tests."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name).resolve()
        self._env_patcher = patch.dict("os.environ", {"AUTO_APPROVE_EDITS": "true"})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_create_writes_file_contents(self):
        result = tools.handle_text_editor(
            {"command": "create", "path": "hello.txt", "file_text": "hi there\n"},
            self.root,
        )
        self.assertEqual(result, "Created hello.txt")
        self.assertEqual((self.root / "hello.txt").read_text(), "hi there\n")

    def test_create_makes_parent_directories(self):
        tools.handle_text_editor(
            {"command": "create", "path": "a/b/c.txt", "file_text": "x"},
            self.root,
        )
        self.assertEqual((self.root / "a" / "b" / "c.txt").read_text(), "x")

    def test_create_backs_up_existing_file(self):
        target = self.root / "file.txt"
        target.write_text("original")
        tools.handle_text_editor(
            {"command": "create", "path": "file.txt", "file_text": "new"},
            self.root,
        )
        self.assertEqual(target.read_text(), "new")
        self.assertEqual((self.root / "file.txt.bak").read_text(), "original")

    def test_view_returns_numbered_lines(self):
        (self.root / "f.txt").write_text("one\ntwo\nthree")
        result = tools.handle_text_editor({"command": "view", "path": "f.txt"}, self.root)
        self.assertEqual(result, "1\tone\n2\ttwo\n3\tthree")

    def test_view_with_range_slices_correctly(self):
        (self.root / "f.txt").write_text("one\ntwo\nthree\nfour")
        result = tools.handle_text_editor(
            {"command": "view", "path": "f.txt", "view_range": [2, 3]}, self.root
        )
        self.assertEqual(result, "2\ttwo\n3\tthree")

    def test_str_replace_succeeds_on_single_match(self):
        (self.root / "f.txt").write_text("hello world")
        result = tools.handle_text_editor(
            {"command": "str_replace", "path": "f.txt", "old_str": "world", "new_str": "there"},
            self.root,
        )
        self.assertEqual(result, "Edited f.txt")
        self.assertEqual((self.root / "f.txt").read_text(), "hello there")

    def test_str_replace_errors_on_zero_matches(self):
        (self.root / "f.txt").write_text("hello world")
        result = tools.handle_text_editor(
            {"command": "str_replace", "path": "f.txt", "old_str": "nope", "new_str": "x"},
            self.root,
        )
        self.assertEqual(result, "Error: old_str not found in f.txt")
        # file must be untouched
        self.assertEqual((self.root / "f.txt").read_text(), "hello world")

    def test_str_replace_errors_on_multiple_matches(self):
        (self.root / "f.txt").write_text("dup dup dup")
        result = tools.handle_text_editor(
            {"command": "str_replace", "path": "f.txt", "old_str": "dup", "new_str": "x"},
            self.root,
        )
        self.assertEqual(result, "Error: old_str matches 3 times in f.txt - must match exactly once")
        self.assertEqual((self.root / "f.txt").read_text(), "dup dup dup")

    def test_insert_at_line(self):
        (self.root / "f.txt").write_text("one\ntwo\nthree")
        result = tools.handle_text_editor(
            {"command": "insert", "path": "f.txt", "insert_line": 1, "insert_text": "inserted"},
            self.root,
        )
        self.assertEqual(result, "Inserted into f.txt after line 1")
        self.assertEqual((self.root / "f.txt").read_text(), "one\ninserted\ntwo\nthree")

    def test_unknown_command_returns_error(self):
        (self.root / "f.txt").write_text("x")
        result = tools.handle_text_editor({"command": "frobnicate", "path": "f.txt"}, self.root)
        self.assertEqual(result, 'Error: unknown text editor command "frobnicate"')

    def test_path_escaping_root_returns_wrapped_error(self):
        # resolve_within_root() raises ValueError; handle_text_editor()'s
        # broad except is what's supposed to turn that into a plain
        # "Error: ..." string instead of letting it propagate.
        result = tools.handle_text_editor(
            {"command": "view", "path": "../outside.txt"}, self.root
        )
        self.assertTrue(result.startswith("Error: "))
        self.assertIn("outside the project root", result)


class EditApprovalGateTests(unittest.TestCase):
    """The approval gate in front of create/str_replace/insert - separate
    from TextEditorTests above, which patches AUTO_APPROVE_EDITS on so it
    can test what each command *does*. These tests are about the gate
    itself: that view never triggers it, that a decline leaves the
    filesystem untouched, and that an approval lets the write through."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name).resolve()

    def tearDown(self):
        self._tmpdir.cleanup()

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="n")
    def test_create_declined_is_not_written(self, mock_input):
        import os as _os

        _os.environ.pop("AUTO_APPROVE_EDITS", None)
        result = tools.handle_text_editor(
            {"command": "create", "path": "new.txt", "file_text": "hello"}, self.root
        )
        self.assertEqual(result, "Edit declined by the user - not written.")
        self.assertFalse((self.root / "new.txt").exists())
        mock_input.assert_called_once()

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="n")
    def test_str_replace_declined_leaves_file_untouched(self, mock_input):
        import os as _os

        _os.environ.pop("AUTO_APPROVE_EDITS", None)
        (self.root / "f.txt").write_text("hello world")
        result = tools.handle_text_editor(
            {"command": "str_replace", "path": "f.txt", "old_str": "world", "new_str": "there"},
            self.root,
        )
        self.assertEqual(result, "Edit declined by the user - not written.")
        self.assertEqual((self.root / "f.txt").read_text(), "hello world")

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="y")
    def test_create_approved_is_written(self, mock_input):
        import os as _os

        _os.environ.pop("AUTO_APPROVE_EDITS", None)
        result = tools.handle_text_editor(
            {"command": "create", "path": "new.txt", "file_text": "hello"}, self.root
        )
        self.assertEqual(result, "Created new.txt")
        self.assertEqual((self.root / "new.txt").read_text(), "hello")
        mock_input.assert_called_once()

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input")
    def test_view_never_prompts(self, mock_input):
        # view is read-only - it must not go anywhere near _confirm_edit().
        # If it did, this mocked input() (which returns a MagicMock, not
        # "y"/"n") would make that obvious; asserting not-called is the
        # direct check.
        import os as _os

        _os.environ.pop("AUTO_APPROVE_EDITS", None)
        (self.root / "f.txt").write_text("one\ntwo")
        result = tools.handle_text_editor({"command": "view", "path": "f.txt"}, self.root)
        self.assertEqual(result, "1\tone\n2\ttwo")
        mock_input.assert_not_called()

    @patch.dict("os.environ", {"AUTO_APPROVE_EDITS": "true"})
    def test_auto_approve_edits_skips_the_prompt(self):
        # No input() mock at all - if the gate tried to prompt, this would
        # hang or raise, not silently pass.
        result = tools.handle_text_editor(
            {"command": "create", "path": "new.txt", "file_text": "hi"}, self.root
        )
        self.assertEqual(result, "Created new.txt")


class BashTests(unittest.TestCase):
    """handle_bash() - approval gating, restart, timeout, and truncation."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name).resolve()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_restart_returns_fixed_message(self):
        result = tools.handle_bash({"restart": True}, self.root)
        self.assertEqual(
            result,
            "(nothing to restart - this harness runs each command fresh, no persistent shell session)",
        )

    def test_empty_command_is_an_error(self):
        result = tools.handle_bash({"command": "  "}, self.root)
        self.assertEqual(result, "Error: empty command")

    @patch.dict("os.environ", {"AUTO_APPROVE_BASH": "true"})
    def test_approved_command_runs_and_returns_output(self):
        result = tools.handle_bash({"command": "echo hi"}, self.root)
        self.assertEqual(result, "hi")

    @patch.dict("os.environ", {"AUTO_APPROVE_BASH": "true"})
    def test_command_runs_in_root_as_cwd(self):
        (self.root / "marker.txt").write_text("x")
        result = tools.handle_bash({"command": "ls"}, self.root)
        self.assertEqual(result, "marker.txt")

    @patch.dict("os.environ", {"AUTO_APPROVE_BASH": "true"})
    def test_no_output_returns_placeholder(self):
        result = tools.handle_bash({"command": "true"}, self.root)
        self.assertEqual(result, "(no output)")

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="n")
    def test_declined_command_is_not_run(self, mock_input):
        # Make sure AUTO_APPROVE_BASH isn't set from a leaked test env, so
        # the code path under test is really the input()-prompt one.
        import os as _os

        _os.environ.pop("AUTO_APPROVE_BASH", None)
        result = tools.handle_bash({"command": "echo should-not-run"}, self.root)
        self.assertEqual(result, "Command declined by the user - not run.")
        mock_input.assert_called_once()

    @patch.dict("os.environ", {"AUTO_APPROVE_BASH": "true"})
    @patch("tools.subprocess.run")
    def test_timeout_is_reported_without_waiting(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep 999", timeout=120)
        result = tools.handle_bash({"command": "sleep 999"}, self.root)
        self.assertEqual(result, "Error: command timed out after 120s")

    @patch.dict("os.environ", {"AUTO_APPROVE_BASH": "true"})
    def test_output_is_truncated_past_the_cap(self):
        # Patch the cap down rather than actually generating megabytes of
        # output - same behavior, instant instead of slow.
        with patch.object(tools, "MAX_BASH_OUTPUT_CHARS", 5):
            result = tools.handle_bash({"command": "echo 1234567890"}, self.root)
        self.assertTrue(result.endswith("... (truncated)"))
        self.assertEqual(result, "12345\n... (truncated)")


if __name__ == "__main__":
    unittest.main()
