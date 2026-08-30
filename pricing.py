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
"""

# $ per 1,000,000 tokens, as (input, output), for models this harness might
# realistically be pointed at via CLAUDE_MODEL.
PRICING_PER_MILLION = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Tokens served from the prompt cache bill at roughly 10% of the normal
# input rate - see the "Verifying Cache Hits" pattern in Anthropic's docs.
CACHE_READ_DISCOUNT = 0.1


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read_input_tokens: int = 0
) -> float | None:
    """Estimate the $ cost of one API call.

    Returns None - not 0 - for a model this file has no pricing for, so a
    typo'd or brand-new CLAUDE_MODEL shows up as "unknown" in a report
    instead of silently being counted as free.
    """
    if model not in PRICING_PER_MILLION:
        return None

    input_price, output_price = PRICING_PER_MILLION[model]
    billable_input_tokens = max(input_tokens - cache_read_input_tokens, 0)

    cost = (
        billable_input_tokens * input_price
        + cache_read_input_tokens * input_price * CACHE_READ_DISCOUNT
        + output_tokens * output_price
    ) / 1_000_000
    return round(cost, 6)
