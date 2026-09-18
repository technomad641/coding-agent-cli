"""
pricing.py
----------
Turns the token counts observability.py already logs into an actual
dollar figure, for evals/report.py, session_report.py, and
cost_report.py - and, live, for main.py's own budget guardrail.

This is a point-in-time snapshot, not a live lookup - Anthropic's pricing
page (https://platform.claude.com/docs/en/about-claude/pricing) or the
Models API (`client.models.retrieve(...)`) is the source of truth if these
numbers drift. Every cost figure this project's reports show is an
estimate for exactly that reason - close enough to compare "was this run
cheaper than the last one," not a substitute for your actual invoice.

Rates below were last checked against that page on 2026-09-18. Two things
that snapshot got wrong before then, both worth knowing about because
they're the failure modes a hardcoded price table actually has:

  - claude-sonnet-5 was listed at $3/$15, a price that never took effect.
    Anthropic had announced $2/$10 as introductory pricing with a rise to
    $3/$15 scheduled for 2026-09-01; that rise was cancelled and $2/$10
    became the standard rate. A table written from the announcement
    instead of the outcome overstated Sonnet 5 by 50%.
  - Several shipping models were missing entirely, so their cost rendered
    as "?" rather than a number.
"""

import re

# $ per 1,000,000 tokens, as (input, output), for models this harness might
# realistically be pointed at via CLAUDE_MODEL. Retired models (Opus 4.1
# and earlier, Sonnet 4, Haiku 3.5) are deliberately absent - pointing this
# harness at one isn't a thing to price, it's a thing to notice, and
# estimate_cost_usd() returning None surfaces it.
PRICING_PER_MILLION = {
    # Fable / Mythos tier
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    # Opus tier
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    # Sonnet tier
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    # Haiku tier
    "claude-haiku-4-5": (1.00, 5.00),
}

# Tokens served from the prompt cache bill at a fraction of the normal
# input rate. 0.1x is the standard multiplier across the lineup - but not
# universally, which is why this is a per-model lookup with a default
# rather than the single flat constant it used to be.
DEFAULT_CACHE_READ_MULTIPLIER = 0.1
CACHE_READ_MULTIPLIER_OVERRIDES = {
    "claude-fable-5-1": 0.025,
    "claude-mythos-5-1": 0.025,
}

# Dated snapshot ids ("claude-haiku-4-5-20251001") are the same model at
# the same price as their base id ("claude-haiku-4-5") - the API reports
# whichever one the request used, so a log full of snapshot ids would
# otherwise price as "unknown" against a table keyed only on base ids.
# That is exactly what happened: every eval run pointed at a snapshot id
# reported "?" for cost until this fallback existed.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def _base_model_id(model: str) -> str:
    """Strip a trailing -YYYYMMDD snapshot suffix, if there is one."""
    return _SNAPSHOT_SUFFIX.sub("", model or "")


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read_input_tokens: int = 0
) -> float | None:
    """Estimate the $ cost of one API call.

    Returns None - not 0 - for a model this file has no pricing for, so a
    typo'd or brand-new CLAUDE_MODEL shows up as "unknown" in a report
    instead of silently being counted as free.
    """
    key = model if model in PRICING_PER_MILLION else _base_model_id(model)
    if key not in PRICING_PER_MILLION:
        return None

    input_price, output_price = PRICING_PER_MILLION[key]
    cache_multiplier = CACHE_READ_MULTIPLIER_OVERRIDES.get(key, DEFAULT_CACHE_READ_MULTIPLIER)
    billable_input_tokens = max(input_tokens - cache_read_input_tokens, 0)

    cost = (
        billable_input_tokens * input_price
        + cache_read_input_tokens * input_price * cache_multiplier
        + output_tokens * output_price
    ) / 1_000_000
    return round(cost, 6)
