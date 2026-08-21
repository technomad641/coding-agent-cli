"""
observability.py
-----------------
A minimal, dependency-free event logger. Every meaningful thing that
happens during a run - an API call, a tool call, a turn starting and
ending, an error - gets written as one JSON line to logs/events.jsonl.

Why a flat JSONL file instead of a "real" tracing stack (OpenTelemetry,
Honeycomb, Langfuse, ...): those all solve the same problem this does -
"what happened, in what order, how long did it take" - just at a scale and
with a UI this project doesn't need. A JSONL file is `grep`-able,
`jq`-able, and requires reading zero new concepts to understand what it's
doing. See the README's Observability section for what you'd reach for
instead once this outgrows a single machine or a single person reading the
log file directly.
"""

import json
import time
import uuid
from pathlib import Path

LOG_PATH = Path("logs") / "events.jsonl"

# Tool results can contain a whole file's contents or a command's full
# output. Logging that in full would make the log file balloon in size and
# risks writing sensitive file contents to disk a second time - so anything
# logged as a "preview" field is truncated to this many characters.
PREVIEW_CHARS = 300


def new_trace_id() -> str:
    """One trace_id per user turn. Every event produced while handling that
    turn (the API call, every tool call, the final summary) is tagged with
    the same trace_id, so `grep <trace_id> logs/events.jsonl` reconstructs
    one full turn, in order, from a flat file with no other tooling."""
    return uuid.uuid4().hex[:12]


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """Truncate long text for logging - see the module docstring for why."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars total)"


def log_event(event: str, trace_id: str, **fields) -> None:
    """Append one structured event to logs/events.jsonl.

    `event` is a short name - "turn_start", "api_call", "tool_call",
    "turn_end", "error" are the ones this harness emits (see main.py).
    `fields` is whatever's relevant to that particular event type; there's
    no fixed schema across event types, which is fine for a log you read
    with `jq` rather than load into a table.
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),  # unix timestamp - sortable, diffable, no timezone ambiguity
        "trace_id": trace_id,
        "event": event,
        **fields,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        # default=str: if a field ever contains something json.dumps can't
        # serialize (a Path, an exception object), stringify it instead of
        # crashing the run just because logging failed.
        f.write(json.dumps(record, default=str) + "\n")


class timer:
    """Tiny context manager for measuring how long something took.

        with timer() as t:
            do_something_slow()
        print(t.ms)  # elapsed milliseconds, set once the `with` block exits

    perf_counter() (not time.time()) because it's monotonic - immune to the
    system clock changing mid-measurement, which matters for a duration but
    not for the timestamp in log_event() above.
    """

    def __enter__(self) -> "timer":
        self._start = time.perf_counter()
        self.ms: float | None = None
        return self

    def __exit__(self, *exc_info) -> bool:
        self.ms = round((time.perf_counter() - self._start) * 1000, 1)
        return False  # False = don't swallow any exception that occurred
