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
