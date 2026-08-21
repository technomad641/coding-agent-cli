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


def run_turn(user_input: str) -> None:
    """Handle one task from the user, start to finish - including however
    many tool calls Claude makes along the way before it's done."""
    messages.append({"role": "user", "content": user_input})

    while True:
        # Stream the response so text prints to the terminal as it's
        # generated, instead of waiting for the whole reply at once.
        # get_final_message() then hands back the complete message when
        # streaming finishes - same shape as a non-streaming response.
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            message = stream.get_final_message()
        print()

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
            result = execute_tool(block.name, block.input)
            print(result)
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


# ---------------------------------------------------------------------------
# The REPL
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"coding-agent-cli - basic coding harness ({MODEL})")
    print(f"project root: {ROOT}")
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

        try:
            run_turn(user_input)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except anthropic.AuthenticationError:
            print("Authentication failed - check ANTHROPIC_API_KEY in .env.")
        except anthropic.RateLimitError:
            print("Rate limited - wait a moment and try again.")
        except anthropic.APIError as err:
            print(f"API error: {err}")


if __name__ == "__main__":
    main()
