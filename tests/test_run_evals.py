"""
tests/test_run_evals.py
-------------------------
Unit tests for evals/run_evals.py's reliability aggregation - run_task()
itself always makes real API calls via a real subprocess (that's the whole
point of it), so it's mocked out here rather than exercised for real; the
real, live, end-to-end path is what evals/run_evals.py IS, and running it
for real (see WORKLOG.md) is how a real bug in it actually got caught.

These tests exist to lock that kind of bug in as a fast, free regression
check going forward: run_task_repeated()'s summarization logic (pass
counts, cost/token sums, the "passed" boolean's strict all-attempts
meaning, the reliability-focused reason string) and _append_history()'s
schema (old-style entries with no "repeats" key must still round-trip,
since evals/history.jsonl is a real file people already have on disk).

evals/run_evals.py has no module-level side effects (unlike main.py -
argparse and the .env/API-key check both happen inside main()), so it's
safely importable here the same way tools.py and cost_report.py are.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import run_evals  # noqa: E402 - needs the path insert above first


def _attempt(passed, reason="ok", tool_calls=1, input_tokens=100, output_tokens=10, cost_usd=0.001, model="m"):
    return {
        "name": "t",
        "passed": passed,
        "reason": reason,
        "model": model,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "duration_ms": 100.0,
        "cost_usd": cost_usd,
    }


class RunTaskRepeatedTests(unittest.TestCase):
    def _repeated(self, attempts):
        with patch.object(run_evals, "run_task", side_effect=list(attempts)):
            return run_evals.run_task_repeated({"name": "t"}, {}, len(attempts))

    def test_all_pass_is_fully_reliable(self):
        result = self._repeated([_attempt(True), _attempt(True), _attempt(True)])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["pass_count"], 3)
        self.assertEqual(result["success_rate"], 1.0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "ok")

    def test_one_failure_makes_it_not_fully_reliable(self):
        # This is the exact shape of bug --repeats is for: a task that
        # passes most of the time but not always must not be reported as
        # "passed" the way a single flaky success would be.
        result = self._repeated([_attempt(True), _attempt(False, reason="file missing"), _attempt(True)])
        self.assertEqual(result["pass_count"], 2)
        self.assertAlmostEqual(result["success_rate"], 2 / 3)
        self.assertFalse(result["passed"])  # strict: not EVERY attempt passed
        self.assertIn("flaky", result["reason"])
        self.assertIn("file missing", result["reason"])

    def test_deterministic_total_failure_is_reported_plainly(self):
        # The exact bug this suite exists because of: every attempt failing
        # for the same reason (a systemic bug, not flakiness) must still
        # summarize clearly, not get mistaken for "some" flakiness.
        result = self._repeated([_attempt(False, reason="declined"), _attempt(False, reason="declined")])
        self.assertEqual(result["pass_count"], 0)
        self.assertEqual(result["success_rate"], 0.0)
        self.assertFalse(result["passed"])
        self.assertIn("2/2 failed", result["reason"])

    def test_costs_and_tokens_sum_across_attempts(self):
        result = self._repeated([_attempt(True, cost_usd=0.01, tool_calls=2), _attempt(True, cost_usd=0.02, tool_calls=3)])
        self.assertAlmostEqual(result["cost_usd"], 0.03)
        self.assertEqual(result["tool_calls"], 5)

    def test_unpriced_attempts_yield_no_cost_not_zero_cost(self):
        # None (unpriced) must not silently become $0 - same "unknown, not
        # free" distinction this project draws everywhere else pricing.py
        # returns None (see run_evals.py's own _fmt_cost, cost_report.py).
        result = self._repeated([_attempt(True, cost_usd=None), _attempt(True, cost_usd=None)])
        self.assertIsNone(result["cost_usd"])

    def test_single_repeat_matches_a_bare_pass_or_fail(self):
        # repeats=1 must be indistinguishable in outcome from the
        # pre---repeats behavior - a passed single attempt is fully
        # "passed", a failed one is not.
        passed = self._repeated([_attempt(True)])
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["success_rate"], 1.0)

        failed = self._repeated([_attempt(False, reason="boom")])
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["success_rate"], 0.0)


class AppendHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_history_path = run_evals.HISTORY_PATH
        run_evals.HISTORY_PATH = Path(self._tmpdir.name) / "history.jsonl"

    def tearDown(self):
        run_evals.HISTORY_PATH = self._orig_history_path
        self._tmpdir.cleanup()

    def _read_last_record(self):
        lines = run_evals.HISTORY_PATH.read_text().splitlines()
        return json.loads(lines[-1])

    def test_records_repeats_and_pooled_attempt_totals(self):
        results = [
            {
                "name": "a", "attempts": 3, "pass_count": 2, "passed": False, "reason": "flaky",
                "model": "m", "tool_calls": 1, "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 0, "duration_ms": 10.0, "cost_usd": 0.01,
            },
            {
                "name": "b", "attempts": 3, "pass_count": 3, "passed": True, "reason": "ok",
                "model": "m", "tool_calls": 2, "input_tokens": 200, "output_tokens": 20,
                "cache_read_input_tokens": 0, "duration_ms": 20.0, "cost_usd": 0.02,
            },
        ]
        run_evals._append_history(results, repeats=3)
        record = self._read_last_record()
        self.assertEqual(record["repeats"], 3)
        self.assertEqual(record["passed"], 1)  # only task "b" was fully reliable
        self.assertEqual(record["total"], 2)
        self.assertEqual(record["accuracy"], 0.5)
        self.assertEqual(record["total_attempts"], 6)
        self.assertEqual(record["total_pass_count"], 5)

    def test_repeats_1_matches_pre_repeats_history_shape(self):
        # The whole point of keeping field names/semantics identical: a
        # repeats=1 run (today's default, unchanged behavior) must produce
        # exactly the same passed/total/accuracy a pre---repeats history
        # entry would have - evals/report.py reads only these run-level
        # fields (never r["tasks"]), so this is what actually keeps old
        # and new history entries comparable on the same trend chart.
        results = [
            {
                "name": "a", "attempts": 1, "pass_count": 1, "passed": True, "reason": "ok",
                "model": "m", "tool_calls": 1, "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 0, "duration_ms": 10.0, "cost_usd": 0.01,
            },
            {
                "name": "b", "attempts": 1, "pass_count": 0, "passed": False, "reason": "boom",
                "model": "m", "tool_calls": 1, "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 0, "duration_ms": 10.0, "cost_usd": 0.01,
            },
        ]
        run_evals._append_history(results, repeats=1)
        record = self._read_last_record()
        self.assertEqual(record["repeats"], 1)
        self.assertEqual(record["passed"], 1)
        self.assertEqual(record["total"], 2)
        self.assertEqual(record["accuracy"], 0.5)
        self.assertEqual(record["total_attempts"], 2)
        self.assertEqual(record["total_pass_count"], 1)


if __name__ == "__main__":
    unittest.main()

    def test_records_requested_model_distinct_from_served_model(self):
        # These are two different facts and the comparison feature depends
        # on not conflating them: "model" is what the API actually served
        # (read back out of the run's own event log), "requested_model" is
        # what --models asked for. A fallback or an alias makes them
        # differ, and a history reader that only had the served id could
        # not tell which arm of a comparison a line belonged to.
        results = [
            {
                "name": "a", "attempts": 1, "pass_count": 1, "passed": True, "reason": "ok",
                "model": "claude-haiku-4-5-20251001", "tool_calls": 1, "input_tokens": 100,
                "output_tokens": 10, "cache_read_input_tokens": 0, "duration_ms": 10.0, "cost_usd": 0.01,
            },
        ]
        run_evals._append_history(results, repeats=1, requested_model="claude-haiku-4-5")
        record = self._read_last_record()
        self.assertEqual(record["requested_model"], "claude-haiku-4-5")
        self.assertEqual(record["model"], "claude-haiku-4-5-20251001")

    def test_requested_model_defaults_to_none(self):
        # Every history line written before --models existed has no
        # requested_model at all, so a plain run must read back the same
        # way those do rather than inventing a value.
        results = [
            {
                "name": "a", "attempts": 1, "pass_count": 1, "passed": True, "reason": "ok",
                "model": "m", "tool_calls": 1, "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 0, "duration_ms": 10.0, "cost_usd": 0.01,
            },
        ]
        run_evals._append_history(results, repeats=1)
        self.assertIsNone(self._read_last_record()["requested_model"])


def _task_result(name, passed=True, model="m", input_tokens=100, output_tokens=10, cost_usd=0.001):
    return {
        "name": name, "attempts": 1, "pass_count": 1 if passed else 0,
        "success_rate": 1.0 if passed else 0.0, "passed": passed,
        "reason": "ok" if passed else "boom", "model": model, "tool_calls": 1,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_input_tokens": 0, "duration_ms": 1000.0, "cost_usd": cost_usd,
    }


class ModelComparisonTests(unittest.TestCase):
    """Comparative runs (--models). run_task() is mocked out for the same
    reason as above - these lock in the bookkeeping around the real runs,
    not the runs themselves."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_history_path = run_evals.HISTORY_PATH
        run_evals.HISTORY_PATH = Path(self._tmpdir.name) / "history.jsonl"

    def tearDown(self):
        run_evals.HISTORY_PATH = self._orig_history_path
        self._tmpdir.cleanup()

    def _capture(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn(*args)
        return result, buf.getvalue()

    def test_each_model_gets_its_own_history_line(self):
        # The design decision this guards: a comparison run is N ordinary
        # runs plus a summary table, not a new kind of run. That's what
        # keeps evals/report.py's trend chart working with no changes -
        # it reads history lines, and it still gets one per model.
        tasks = [{"name": "t1"}, {"name": "t2"}]
        with patch.object(run_evals, "TASKS", tasks), \
                patch.object(run_evals, "run_task", side_effect=lambda t, e: _attempt(True)):
            for model in ("model-a", "model-b"):
                self._capture(run_evals.run_and_report, {"CLAUDE_MODEL": model}, 1, model)

        records = [json.loads(line) for line in run_evals.HISTORY_PATH.read_text().splitlines()]
        self.assertEqual([r["requested_model"] for r in records], ["model-a", "model-b"])
        self.assertTrue(all(r["total"] == 2 for r in records))

    def test_run_and_report_returns_per_task_results_for_the_table(self):
        tasks = [{"name": "t1"}, {"name": "t2"}]
        with patch.object(run_evals, "TASKS", tasks), \
                patch.object(run_evals, "run_task", side_effect=lambda t, e: _attempt(True)):
            results, _ = self._capture(run_evals.run_and_report, {}, 1, "model-a")
        self.assertEqual([r["name"] for r in results], ["t1", "t2"])

    def test_run_suite_passes_the_models_env_through_to_every_task(self):
        # --models works by overriding CLAUDE_MODEL in the subprocess env.
        # If run_suite() dropped or rebuilt that env, every "comparison"
        # would silently be the same model N times - the failure mode that
        # would make the entire feature quietly meaningless.
        seen_envs = []

        def _spy(task, env):
            seen_envs.append(env)
            return _attempt(True)

        with patch.object(run_evals, "TASKS", [{"name": "t1"}, {"name": "t2"}]), \
                patch.object(run_evals, "run_task", side_effect=_spy):
            self._capture(run_evals.run_suite, {"CLAUDE_MODEL": "model-a"}, 1)

        self.assertEqual([e["CLAUDE_MODEL"] for e in seen_envs], ["model-a", "model-a"])

    def test_comparison_table_has_one_row_per_requested_model(self):
        # Grouped on the *requested* id, not the served one: two aliases
        # that resolve to the same served model are still two arms of the
        # experiment and must not collapse into one row.
        per_model = [
            ("model-a", [_task_result("t1", passed=True, model="served-x")]),
            ("model-b", [_task_result("t1", passed=False, model="served-x")]),
        ]
        _, out = self._capture(run_evals._print_model_comparison, per_model, 1)
        self.assertIn("model-a", out)
        self.assertIn("model-b", out)
        self.assertIn("1/1 (100%)", out)
        self.assertIn("0/1 (0%)", out)

    def test_comparison_notes_a_served_model_that_differs_from_the_request(self):
        per_model = [("claude-haiku-4-5", [_task_result("t1", model="claude-haiku-4-5-20251001")])]
        _, out = self._capture(run_evals._print_model_comparison, per_model, 1)
        self.assertIn("note:", out)
        self.assertIn("claude-haiku-4-5-20251001", out)

    def test_comparison_is_quiet_when_the_served_model_matches(self):
        per_model = [("model-a", [_task_result("t1", model="model-a")])]
        _, out = self._capture(run_evals._print_model_comparison, per_model, 1)
        self.assertNotIn("note:", out)

    def test_comparison_relabels_accuracy_as_reliability_when_repeating(self):
        # At repeats>1 the column means "tasks where every attempt passed",
        # which is a stricter claim than "accuracy" - saying so in the
        # header is the same honesty fix already made for the task table.
        per_model = [("model-a", [_task_result("t1")])]
        _, single = self._capture(run_evals._print_model_comparison, per_model, 1)
        _, repeated = self._capture(run_evals._print_model_comparison, per_model, 5)

        # Assert on the header row itself, not just on the output as a
        # whole: the repeats>1 footnote also contains the word RELIABLE,
        # so a loose assertIn over everything passes even when the header
        # never changes at all. (It did, until this test was mutation-
        # tested and found to be checking the footnote twice.)
        def _header(out):
            # The column header, not the "MODEL COMPARISON" banner above
            # it - both start with MODEL, and picking the banner would
            # make every assertion below vacuously true again.
            return next(line for line in out.splitlines() if "TOKENS" in line)

        self.assertIn("ACCURACY", _header(single))
        self.assertNotIn("RELIABLE", _header(single))
        self.assertIn("RELIABLE", _header(repeated))
        self.assertNotIn("ACCURACY", _header(repeated))

        # The footnote explaining the stricter column, present only where
        # it applies - at 1x it would read "all 1 attempts passed", which
        # is noise at best and implies a repeated run at worst.
        self.assertIn("all 5 attempts passed", repeated)
        self.assertNotIn("attempts passed", single)

    def test_comparison_reports_numbers_without_crowning_a_winner(self):
        # Deliberate: 4 golden tasks is far too thin a sample to pick a
        # model on, and which tradeoff wins depends on what you're
        # optimizing for. The table informs; it does not decide.
        per_model = [
            ("cheap-model", [_task_result("t1", passed=False, cost_usd=0.001)]),
            ("pricey-model", [_task_result("t1", passed=True, cost_usd=0.10)]),
        ]
        _, out = self._capture(run_evals._print_model_comparison, per_model, 1)
        for verdict in ("winner", "best", "recommend", "worst"):
            self.assertNotIn(verdict, out.lower())
