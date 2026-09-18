"""
tests/test_pricing.py
-----------------------
Unit tests for pricing.py - the snapshot-id fallback, the per-model cache
multiplier, and the "unknown model is None, not zero" rule.

pricing.py is a hardcoded point-in-time table, so these tests deliberately
do NOT assert specific dollar rates for specific models: that would just
encode today's price list twice and turn every real price change into a
test failure in two places. What they lock in is the *logic* around the
table, which is where the bugs actually were - a snapshot id pricing as
"unknown", and one flat cache multiplier applied to models that don't all
share it.
"""

import unittest

import pricing


class SnapshotIdTests(unittest.TestCase):
    """A dated snapshot id must price identically to its base id - the
    real gap this fixed: every eval run pointed at a snapshot id showed
    "?" for cost, because the API reports back the id the request used."""

    def test_snapshot_id_prices_same_as_base_id(self):
        base = pricing.estimate_cost_usd("claude-haiku-4-5", 1000, 100)
        snapshot = pricing.estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 100)
        self.assertIsNotNone(base)
        self.assertEqual(base, snapshot)

    def test_exact_match_wins_over_stripping(self):
        # A base id that's in the table directly must not be rewritten.
        self.assertIsNotNone(pricing.estimate_cost_usd("claude-opus-5", 1000, 100))

    def test_unknown_model_is_none_not_zero(self):
        self.assertIsNone(pricing.estimate_cost_usd("definitely-not-a-model", 1000, 100))

    def test_unknown_model_with_snapshot_suffix_is_still_none(self):
        self.assertIsNone(pricing.estimate_cost_usd("not-a-model-20260101", 1000, 100))

    def test_none_model_is_none_not_a_crash(self):
        self.assertIsNone(pricing.estimate_cost_usd(None, 1000, 100))

    def test_base_id_helper_only_strips_an_8_digit_suffix(self):
        self.assertEqual(pricing._base_model_id("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        # a version-ish suffix that isn't a date must survive untouched
        self.assertEqual(pricing._base_model_id("claude-sonnet-4-6"), "claude-sonnet-4-6")
        self.assertEqual(pricing._base_model_id("claude-opus-5"), "claude-opus-5")


class CacheMultiplierTests(unittest.TestCase):
    def test_cached_tokens_cost_less_than_uncached(self):
        uncached = pricing.estimate_cost_usd("claude-opus-5", 10_000, 0, cache_read_input_tokens=0)
        cached = pricing.estimate_cost_usd("claude-opus-5", 10_000, 0, cache_read_input_tokens=10_000)
        self.assertLess(cached, uncached)

    def test_override_model_gets_a_cheaper_cache_read_than_the_default(self):
        # Fable 5.1 reads cache at 0.025x where the rest of the lineup is
        # 0.1x - same base input price, so an all-cache-read call must
        # come out strictly cheaper than a default-multiplier model at the
        # same base rate would.
        override_model = "claude-fable-5-1"
        default_model = "claude-fable-5"
        self.assertEqual(
            pricing.PRICING_PER_MILLION[override_model],
            pricing.PRICING_PER_MILLION[default_model],
            "this test only means anything while both share a base rate",
        )
        override = pricing.estimate_cost_usd(override_model, 10_000, 0, cache_read_input_tokens=10_000)
        default = pricing.estimate_cost_usd(default_model, 10_000, 0, cache_read_input_tokens=10_000)
        self.assertLess(override, default)

    def test_cache_reads_are_never_double_counted_as_billable_input(self):
        # cache_read_input_tokens is a subset of input_tokens, so billable
        # input is the difference - never negative, even if a log somehow
        # reports more cache reads than inputs.
        cost = pricing.estimate_cost_usd("claude-opus-5", 100, 0, cache_read_input_tokens=10_000)
        self.assertGreaterEqual(cost, 0)


if __name__ == "__main__":
    unittest.main()
