"""
tests/test_cost_report.py
--------------------------
Unit tests for cost_report.py, in isolation - calling aggregate() and
render_report() directly with hand-built events, no real logs/events.jsonl,
no API key.

Unlike main.py, cost_report.py has no module-level side effects (argparse
and the report-writing all happen inside main(), guarded by
`if __name__ == "__main__":`), so it's safely importable and testable the
same way tools.py is - see tests/test_tools.py's module docstring for the
general "why unit tests here, separate from evals/run_evals.py" reasoning.

These tests exist in part because live testing this feature (see
WORKLOG.md) caught a real bug - a local variable named `days` inside
render_report() shadowing the `days` parameter, so the footer text
rendered a list of date strings instead of a day count - that a test built
around the actual rendered HTML text would have caught immediately. A
couple of these tests check for that exact regression.
"""

import unittest

import cost_report


def _api_call(session_id, ts, model, input_tokens, output_tokens, cache_read=0):
    return {
        "event": "api_call",
        "session_id": session_id,
        "ts": ts,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
    }


def _turn_start(session_id, ts):
    return {"event": "turn_start", "session_id": session_id, "ts": ts}


class AggregateTests(unittest.TestCase):
    def test_sums_tokens_and_cost_per_session(self):
        events = [
            _turn_start("s1", 1000.0),
            _api_call("s1", 1000.0, "claude-sonnet-5", 1000, 100),
            _turn_start("s1", 1001.0),
            _api_call("s1", 1001.0, "claude-sonnet-5", 500, 50),
        ]
        agg = cost_report.aggregate(events)
        s = agg["sessions"]["s1"]
        self.assertEqual(s["input_tokens"], 1500)
        self.assertEqual(s["output_tokens"], 150)
        self.assertEqual(s["turns"], 2)
        self.assertGreater(s["cost"], 0)
        self.assertFalse(s["has_unpriced"])
        self.assertEqual(agg["unpriced_calls"], 0)

    def test_unpriced_model_is_tracked_separately(self):
        events = [
            _turn_start("s1", 1000.0),
            _api_call("s1", 1000.0, "some-model-not-in-pricing", 1000, 100),
        ]
        agg = cost_report.aggregate(events)
        s = agg["sessions"]["s1"]
        self.assertEqual(s["cost"], 0.0)
        self.assertTrue(s["has_unpriced"])
        self.assertEqual(agg["unpriced_calls"], 1)
        self.assertEqual(agg["by_day"], {})  # nothing priced -> no day bucket

    def test_separates_sessions_and_buckets_by_day(self):
        # Two sessions, two different days (real epoch seconds a day apart).
        day1 = 1735689600.0  # 2025-01-01 00:00:00 UTC
        day2 = day1 + 86400
        events = [
            _turn_start("s1", day1),
            _api_call("s1", day1, "claude-sonnet-5", 1000, 100),
            _turn_start("s2", day2),
            _api_call("s2", day2, "claude-sonnet-5", 1000, 100),
        ]
        agg = cost_report.aggregate(events)
        self.assertEqual(set(agg["sessions"].keys()), {"s1", "s2"})
        self.assertEqual(len(agg["by_day"]), 2)
        # same tokens on both days -> same cost bucketed per day
        self.assertAlmostEqual(list(agg["by_day"].values())[0], list(agg["by_day"].values())[1])

    def test_non_api_call_events_are_ignored_for_cost(self):
        events = [
            _turn_start("s1", 1000.0),
            {"event": "tool_call", "session_id": "s1", "ts": 1000.0, "tool_name": "bash"},
            {"event": "turn_end", "session_id": "s1", "ts": 1000.0},
        ]
        agg = cost_report.aggregate(events)
        self.assertEqual(agg["sessions"], {})  # no api_call events -> no sessions tracked


class RenderReportTests(unittest.TestCase):
    """Checking the actual rendered HTML text, not just that render_report()
    returns without raising - this is exactly the level a shadowed-variable
    bug like the one WORKLOG.md describes would have been caught at."""

    def _agg(self):
        events = [
            _turn_start("s1", 1000.0),
            _api_call("s1", 1000.0, "claude-sonnet-5", 1000, 100),
        ]
        return cost_report.aggregate(events)

    def test_empty_sessions_renders_empty_state(self):
        html = cost_report.render_report({"sessions": {}, "by_day": {}, "unpriced_calls": 0}, days=None)
        self.assertIn("No api_call events yet", html)

    def test_all_time_lede_says_every_session(self):
        html = cost_report.render_report(self._agg(), days=None)
        lede = html.split('<p class="lede">')[1].split("</p>")[0]
        self.assertIn("every session on record", lede)
        # the exact regression this test is here for: with days=None, the
        # lede must say the words above - not a Python list/None literal
        # like "['2026-08-30']" (what a shadowed `days` variable produced).
        self.assertNotIn("[", lede)
        self.assertNotIn("None", lede)

    def test_days_filter_lede_says_last_n_days(self):
        html = cost_report.render_report(self._agg(), days=7)
        self.assertIn("last 7 day(s)", html)

    def test_unpriced_session_gets_plus_marker_in_table(self):
        agg = cost_report.aggregate(
            [
                _turn_start("s1", 1000.0),
                _api_call("s1", 1000.0, "unknown-model", 1000, 100),
            ]
        )
        html = cost_report.render_report(agg, days=None)
        self.assertIn("+</td>", html)

    def test_priced_session_has_no_plus_marker(self):
        html = cost_report.render_report(self._agg(), days=None)
        self.assertNotIn("+</td>", html)


class DaysValidationTests(unittest.TestCase):
    """--days 0 is a real footgun: `if args.days` treats 0 the same as
    "not given" (Python truthiness), which would silently mean "all time"
    instead of what someone typing `--days 0` would expect. main.py's
    argparse setup rejects it outright - checked here via subprocess so
    it's the actual CLI behavior under test, not just the validation
    function in isolation."""

    def _run(self, *args):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, cost_report.__file__, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_days_zero_is_rejected(self):
        result = self._run("--days", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a positive integer", result.stderr)

    def test_negative_days_is_rejected(self):
        result = self._run("--days", "-3")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a positive integer", result.stderr)


if __name__ == "__main__":
    unittest.main()
