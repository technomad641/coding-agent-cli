"""
tests/test_eval_report.py
--------------------------
Unit tests for evals/report.py's model-grouping logic.

The rendering itself is verified by looking at the generated HTML in a
browser (see WORKLOG.md) - what's worth a fast regression check is the
grouping underneath it, because getting it wrong produces a report that
is confidently, silently wrong rather than visibly broken: two arms of a
comparison merged into one bar, or one model split across two.

evals/report.py reads evals/history.jsonl only inside load_history(), so
the functions below are pure and safely importable.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import report  # noqa: E402 - needs the path insert above first


def _run(ts, accuracy=1.0, model=None, requested_model=None, cost=0.01, repeats=1):
    run = {"ts": ts, "accuracy": accuracy, "passed": int(accuracy * 4), "total": 4,
           "total_cost_usd": cost, "total_duration_ms": 1000.0, "repeats": repeats}
    if model is not None:
        run["model"] = model
    if requested_model is not None:
        run["requested_model"] = requested_model
    return run


class RunModelTests(unittest.TestCase):
    def test_prefers_the_requested_model_over_the_served_one(self):
        # The case this exists for: an alias resolving to a dated snapshot
        # id. Grouping on what the API served would split one model's
        # history in two the day the snapshot behind an alias rolls.
        run = _run(1, model="claude-haiku-4-5-20251001", requested_model="claude-haiku-4-5")
        self.assertEqual(report.run_model(run), "claude-haiku-4-5")

    def test_falls_back_to_the_served_model_for_pre_models_history(self):
        # Every history line written before --models existed has no
        # requested_model - those must still group, not all collapse into
        # a single "unknown" bucket.
        self.assertEqual(report.run_model(_run(1, model="claude-sonnet-5")), "claude-sonnet-5")

    def test_unlabeled_runs_are_named_rather_than_crashing(self):
        self.assertEqual(report.run_model(_run(1)), "unknown")


class LatestRunPerModelTests(unittest.TestCase):
    def test_keeps_only_the_most_recent_run_of_each_model(self):
        runs = [
            _run(1, accuracy=0.25, model="a"),
            _run(2, accuracy=0.50, model="b"),
            _run(3, accuracy=1.00, model="a"),
        ]
        latest = report.latest_run_per_model(runs)
        self.assertEqual([report.run_model(r) for r in latest], ["a", "b"])
        self.assertEqual([r["accuracy"] for r in latest], [1.00, 0.50])

    def test_an_old_served_only_run_and_a_new_requested_run_are_one_model(self):
        # A real sequence: some plain runs, then a --models run naming the
        # same model. If these didn't merge, the report would show one
        # model twice under the same name.
        runs = [
            _run(1, accuracy=0.50, model="claude-sonnet-5"),
            _run(2, accuracy=0.75, model="claude-sonnet-5", requested_model="claude-sonnet-5"),
        ]
        latest = report.latest_run_per_model(runs)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["accuracy"], 0.75)

    def test_two_requested_ids_stay_separate_even_when_served_the_same(self):
        runs = [
            _run(1, accuracy=0.25, model="served-x", requested_model="alias-a"),
            _run(2, accuracy=1.00, model="served-x", requested_model="alias-b"),
        ]
        self.assertEqual(len(report.latest_run_per_model(runs)), 2)


class RenderModelComparisonTests(unittest.TestCase):
    def test_hidden_until_history_holds_more_than_one_model(self):
        # A single-model history is exactly what the trend charts above
        # already show; a one-bar "comparison" would be noise.
        self.assertEqual(report.render_model_comparison([_run(1, model="a"), _run(2, model="a")]), "")

    def test_shown_with_a_bar_per_model_once_there_are_two(self):
        html = report.render_model_comparison([
            _run(1, accuracy=0.5, model="a", cost=0.01),
            _run(2, accuracy=1.0, model="b", cost=0.20),
        ])
        self.assertIn("Model comparison", html)
        self.assertIn("2/4 (50%)", html)
        self.assertIn("4/4 (100%)", html)
        self.assertIn("$0.2000", html)

    def test_warns_when_the_models_ran_with_different_repeat_counts(self):
        # Accuracy at 5x means "passed every attempt" and at 1x means
        # "passed once" - comparing those two bars side by side without
        # saying so would be the exact dishonesty the Repeats column of
        # the run-history table was added to avoid.
        mixed = report.render_model_comparison([
            _run(1, model="a", repeats=1),
            _run(2, model="b", repeats=5),
        ])
        same = report.render_model_comparison([
            _run(1, model="a", repeats=1),
            _run(2, model="b", repeats=1),
        ])
        self.assertIn("Repeat counts differ", mixed)
        self.assertNotIn("Repeat counts differ", same)

    def test_survives_runs_with_no_cost_recorded(self):
        # cost is None whenever pricing.py doesn't know a model id, so
        # this is a real state, not a hypothetical - and it's the divisor
        # for every cost bar's width.
        html = report.render_model_comparison([
            _run(1, model="a", cost=None),
            _run(2, model="b", cost=None),
        ])
        self.assertIn("Model comparison", html)


if __name__ == "__main__":
    unittest.main()
