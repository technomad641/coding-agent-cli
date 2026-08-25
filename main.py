"""
main.py
-------
coding-agent-cli - a very basic coding CLI harness, in Python.

This is the manual agentic loop, written out by hand instead of hidden
behind a framework: send messages + tool definitions to Claude -> Claude
replies, optionally asking to run a tool -> we run it locally -> we send
the result back -> repeat until Claude has nothing left to ask for. That's
the entire mechanism behind Claude Code and every other coding agent; this
file just doesn't hide it behind an SDK.

Run it with:

    python main.py

See README.md for the full walkthrough (architecture diagram, a sequence
diagram of one turn, and the threat model for what is and isn't guarded).
"""

import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from tools import TOOLS, handle_bash, handle_text_editor
from observability import new_trace_id, log_event, preview, timer
from pricing import estimate_cost_usd

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# Reads a .env file in the current directory (if one exists) into the
# process's environment variables - this is how ANTHROPIC_API_KEY gets set
# without you having to export it in your shell every time.
load_dotenv()

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("Missing ANTHROPIC_API_KEY - copy .env.example to .env and set it.")
    sys.exit(1)

client = anthropic.Anthropic(api_key=API_KEY)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))

# Adaptive thinking is on by default below, but only these model families
# actually support it - Haiku-tier models reject the request outright
# (see WORKLOG.md for how that was found). Anything not in this set just
# runs without thinking instead of erroring - a valid, simply less-guided
# mode, not a degraded one.
MODELS_WITH_ADAPTIVE_THINKING = {
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}

# The budget guardrail. <= 0 means disabled - no cap, no tracking overhead.
# $1.00 is a deliberately generous default for a small learning project
# (a full 4-task eval run costs well under $0.20 - see WORKLOG.md); it
# exists to catch a genuine runaway loop, not to ration normal use.
SESSION_BUDGET_USD = float(os.environ.get("SESSION_BUDGET_USD", "1.00"))

# Running total for this process. One session = one python main.py run -
# same scope as observability.py's SESSION_ID, and reset to 0 every time
# you start the CLI, since there's no persistence across runs (yet - see
# Known limitations in the README).
session_cost_usd = 0.0
_warned_unpriced_model = False  # print the "can't track cost" warning once, not every turn


class BudgetExceeded(Exception):
    """Raised when session_cost_usd crosses SESSION_BUDGET_USD.

    Caught in main(), which ends the whole session rather than just this
    turn: if we returned to the REPL prompt instead, `messages` could be
    left with an assistant turn's tool_use blocks and no matching
    tool_result (we stop before running them) - the next API call with
    that history would be rejected outright. There's no persistence yet
    (see Known limitations) to safely resume from anyway, so a clean stop
    here is simpler than a half-resumable one.
    """


# The CLI can only see and edit this directory and whatever is below it -
# see tools.py's resolve_within_root() for how that's enforced.
ROOT = Path.cwd().resolve()

SYSTEM_PROMPT = f"""You are a basic coding assistant running as a local CLI harness.
You have two tools: bash (runs shell commands) and a text editor (view/create/str_replace/insert).
Every file operation is confined to the project root: {ROOT}
Every bash command requires the user's interactive y/n approval before it runs - if declined, adapt your plan instead of repeating the same command.
Be direct and concise. When a task is done, stop and summarize what changed instead of continuing to poke around."""

# The whole conversation so far, as a plain list of {"role": ..., "content": ...}
# dicts. The Messages API is stateless - we resend this entire list on every
# request - so this list IS the model's memory of the session. Nothing else
# is stored anywhere.
messages: list[dict] = []


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def execute_tool(name: str, tool_input: dict) -> str:
    """Map a tool_use block's name to the local function that actually runs it."""
    if name == "bash":
        return handle_bash(tool_input, ROOT)
    if name == "str_replace_based_edit_tool":
        return handle_text_editor(tool_input, ROOT)
    return f'Error: unknown tool "{name}"'


def describe_call(name: str, tool_input: dict) -> str:
    """A short line printed before a tool runs, so you can see what's about
    to happen instead of just watching output appear."""
    if name == "bash":
        return str(tool_input.get("command", ""))
    if name == "str_replace_based_edit_tool":
        return f"{tool_input.get('command')} {tool_input.get('path', '')}"
    return str(tool_input)


# ---------------------------------------------------------------------------
# The agentic loop
# ---------------------------------------------------------------------------


def run_turn(trace_id: str, user_input: str) -> None:
    """Handle one task from the user, start to finish - including however
    many tool calls Claude makes along the way before it's done.

    `trace_id` ties every event this turn produces - the API call, every
    tool call, the summary at the end - together in logs/events.jsonl. See
    observability.py and the README's Observability section.
    """
    global session_cost_usd, _warned_unpriced_model

    messages.append({"role": "user", "content": user_input})
    log_event("turn_start", trace_id, user_input=preview(user_input))
    tool_call_count = 0

    with timer() as turn_timer:
        while True:
            # Stream the response so text prints to the terminal as it's
            # generated, instead of waiting for the whole reply at once.
            # get_final_message() then hands back the complete message when
            # streaming finishes - same shape as a non-streaming response.
            request_kwargs = dict(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            if MODEL in MODELS_WITH_ADAPTIVE_THINKING:
                request_kwargs["thinking"] = {"type": "adaptive"}

            with timer() as api_timer, client.messages.stream(**request_kwargs) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
                message = stream.get_final_message()
            print()

            # message.usage carries token counts for this one API call -
            # the raw numbers behind whatever "cost of this turn" you'd
            # want to compute. See the README's Observability section.
            log_event(
                "api_call",
                trace_id,
                model=message.model,
                stop_reason=message.stop_reason,
                latency_ms=api_timer.ms,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                cache_read_input_tokens=message.usage.cache_read_input_tokens,
            )

            # The budget guardrail: tally this call's cost and stop before
            # running anything else - another API call or the tool calls
            # Claude just asked for - the moment the running total reaches
            # the cap. Checked here, not just between turns, so one very
            # long tool-calling turn can't blow past it unnoticed.
            call_cost = estimate_cost_usd(
                message.model,
                message.usage.input_tokens,
                message.usage.output_tokens,
                message.usage.cache_read_input_tokens,
            )
            if call_cost is None:
                if SESSION_BUDGET_USD > 0 and not _warned_unpriced_model:
                    print(f"[no pricing data for {message.model} - the session budget can't track this call's cost]")
                    _warned_unpriced_model = True
            else:
                session_cost_usd += call_cost
                if SESSION_BUDGET_USD > 0 and session_cost_usd >= SESSION_BUDGET_USD:
                    raise BudgetExceeded(
                        f"session cost ${session_cost_usd:.4f} has reached the ${SESSION_BUDGET_USD:.2f} "
                        f"session budget (SESSION_BUDGET_USD in .env) - stopping now, before running "
                        f"anything else."
                    )

            if message.stop_reason == "pause_turn":
                # A server-side tool hit an internal continuation point. This
                # harness's two tools never trigger it, but it's part of the
                # API contract - the fix is just: resend and keep going.
                messages.append({"role": "assistant", "content": message.content})
                continue

            if message.stop_reason == "refusal" and message.stop_details:
                print(f"[declined: {message.stop_details.category or 'policy'}]")
            if message.stop_reason == "max_tokens":
                print(f"[cut off at MAX_TOKENS={MAX_TOKENS} - raise it in .env for longer responses]")

            messages.append({"role": "assistant", "content": message.content})

            # Did Claude ask to run any tools this turn? message.content is a
            # list of typed blocks (text, thinking, tool_use, ...) - we only
            # care about the tool_use ones here.
            tool_use_blocks = [block for block in message.content if block.type == "tool_use"]
            if not tool_use_blocks:
                break  # nothing left to do - stop_reason was end_turn (or refusal/max_tokens)

            # Run every requested tool and collect a tool_result for each one,
            # then send them all back together in a single message - the API
            # expects one tool_result per tool_use, all in the same turn.
            tool_results = []
            for block in tool_use_blocks:
                print(f"\n[{block.name}] {describe_call(block.name, block.input)}")
                with timer() as tool_timer:
                    result = execute_tool(block.name, block.input)
                print(result)
                tool_call_count += 1
                log_event(
                    "tool_call",
                    trace_id,
                    tool_name=block.name,
                    call=preview(describe_call(block.name, block.input), 120),
                    duration_ms=tool_timer.ms,
                    # A heuristic, not a guarantee: our own tool handlers
                    # only ever start a failure or a decline with "Error"
                    # or the fixed decline string - see tools.py. Good
                    # enough to spot a rough success rate at a glance;
                    # don't treat it as a verified pass/fail signal.
                    looks_successful=not result.startswith(("Error", "Command declined")),
                    result_preview=preview(result),
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,  # must match this tool_use block's id
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})
            # Loop back around: Claude sees the tool_result(s) above and decides
            # what to do next - reply, or ask for another tool.

    log_event("turn_end", trace_id, tool_calls=tool_call_count, duration_ms=turn_timer.ms)


# ---------------------------------------------------------------------------
# The REPL
# ---------------------------------------------------------------------------


def main() -> None:
    budget_line = f"${SESSION_BUDGET_USD:.2f}" if SESSION_BUDGET_USD > 0 else "none (disabled)"
    print(f"coding-agent-cli - basic coding harness ({MODEL})")
    print(f"project root: {ROOT}")
    print(f"session budget: {budget_line} (SESSION_BUDGET_USD in .env - 0 disables it)")
    print('type a task, or "exit" to quit\n')

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break  # Ctrl+D or Ctrl+C at the prompt - just exit quietly

        if not user_input:
            continue
        if user_input in ("exit", "quit"):
            break

        # One trace_id per turn, generated here (not inside run_turn) so
        # it's still available in the except block below if run_turn
        # raises before logging anything itself.
        trace_id = new_trace_id()
        try:
            run_turn(trace_id, user_input)
        except BudgetExceeded as err:
            print(f"\n{err}")
            log_event("error", trace_id, kind="budget_exceeded", session_cost_usd=round(session_cost_usd, 4))
            break  # end the session outright, not just this turn - see BudgetExceeded's docstring
        except KeyboardInterrupt:
            print("\n(interrupted)")
            log_event("error", trace_id, kind="interrupted")
        except anthropic.AuthenticationError:
            print("Authentication failed - check ANTHROPIC_API_KEY in .env.")
            log_event("error", trace_id, kind="authentication_error")
        except anthropic.RateLimitError:
            print("Rate limited - wait a moment and try again.")
            log_event("error", trace_id, kind="rate_limit_error")
        except anthropic.APIError as err:
            print(f"API error: {err}")
            log_event("error", trace_id, kind="api_error", message=preview(str(err)))


if __name__ == "__main__":
    main()
