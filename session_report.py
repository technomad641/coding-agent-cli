"""
session_report.py
------------------
Turns one session's worth of logs/events.jsonl into an HTML report:
token usage and estimated cost, broken down turn by turn.

logs/events.jsonl accumulates across every `python main.py` run - it's
not truncated between them - so "one session" means one run, identified
by the session_id observability.py stamps on every event a process logs.
By default this reports on the most recent session in the file.

Run it with:

    python session_report.py                # most recent session
    python session_report.py --all           # list every session found
    python session_report.py --session <id>  # a specific one, from --all
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from pricing import estimate_cost_usd
from report_style import bar_row, chip, empty_state, legend, page, section, stat_row

EVENTS_PATH = Path("logs") / "events.jsonl"
REPORT_PATH = Path("logs") / "session_report.html"


def load_events() -> list[dict]:
    if not EVENTS_PATH.exists():
        print(f"No {EVENTS_PATH} yet - run `python main.py`, do at least one task, then try again.")
        sys.exit(1)
    events = [json.loads(line) for line in EVENTS_PATH.read_text().splitlines() if line.strip()]
    if not events:
        print(f"{EVENTS_PATH} exists but is empty - run `python main.py` and do at least one task.")
        sys.exit(1)
    return events


def list_sessions(events: list[dict]) -> list[tuple[str, float, int]]:
    """Returns (session_id, first_ts, turn_count) for every session in the
    log, oldest first."""
    by_session: dict[str, dict] = {}
    for e in events:
        s = by_session.setdefault(e["session_id"], {"first_ts": e["ts"], "traces": set()})
        s["first_ts"] = min(s["first_ts"], e["ts"])
        if e["event"] == "turn_start":
            s["traces"].add(e["trace_id"])
    return sorted(
        ((sid, info["first_ts"], len(info["traces"])) for sid, info in by_session.items()),
        key=lambda row: row[1],
    )


def build_turns(events: list[dict]) -> list[dict]:
    """Group one session's events by trace_id (= one turn) and reduce each
    group down to the numbers a report actually needs."""
    by_trace: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for e in events:
        if e["trace_id"] not in by_trace:
            order.append(e["trace_id"])
        by_trace[e["trace_id"]].append(e)

    turns = []
    for trace_id in order:
        group = by_trace[trace_id]
        turn = {
            "trace_id": trace_id,
            "task": "",
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "tool_calls": 0,
            "duration_ms": 0.0,
            "error": None,
        }
        for e in group:
            if e["event"] == "turn_start":
                turn["task"] = e.get("user_input", "")
            elif e["event"] == "api_call":
                turn["model"] = turn["model"] or e.get("model")
                turn["input_tokens"] += e.get("input_tokens", 0)
                turn["output_tokens"] += e.get("output_tokens", 0)
                turn["cache_read_input_tokens"] += e.get("cache_read_input_tokens", 0)
            elif e["event"] == "tool_call":
                turn["tool_calls"] += 1
            elif e["event"] == "turn_end":
                turn["duration_ms"] = e.get("duration_ms", turn["duration_ms"])
            elif e["event"] == "error":
                turn["error"] = e.get("kind", "error")
        turn["cost_usd"] = (
            estimate_cost_usd(
                turn["model"], turn["input_tokens"], turn["output_tokens"], turn["cache_read_input_tokens"]
            )
            if turn["model"]
            else None
        )
        turns.append(turn)
    return turns


def fmt_cost(v) -> str:
    return f"${v:.4f}" if v is not None else "—"


def render_report(session_id: str, turns: list[dict]) -> str:
    total_in = sum(t["input_tokens"] for t in turns)
    total_out = sum(t["output_tokens"] for t in turns)
    total_tools = sum(t["tool_calls"] for t in turns)
    total_ms = sum(t["duration_ms"] for t in turns)
    costs = [t["cost_usd"] for t in turns if t["cost_usd"] is not None]
    unpriced = sum(1 for t in turns if t["cost_usd"] is None and t["model"])
    total_cost = sum(costs)

    tiles = stat_row(
        [
            ("Turns", str(len(turns)), f"session {session_id}"),
            ("Est. cost", fmt_cost(total_cost) if costs else "—", f"{unpriced} turn(s) unpriced" if unpriced else "all turns priced"),
            ("Tokens", f"{total_in + total_out:,}", f"{total_in:,} in · {total_out:,} out"),
            ("Tool calls", str(total_tools), f"{total_ms / 1000:.1f}s total"),
        ]
    )

    if not turns:
        body = tiles + section("Turns", "", empty_state("No turns in this session yet."))
    else:
        max_tokens = max((t["input_tokens"] + t["output_tokens"] for t in turns), default=1)
        bars = legend([("input tokens", "var(--blue)"), ("output tokens", "var(--orange)")])
        for i, t in enumerate(turns, start=1):
            label = t["task"][:40] or f"turn {i}"
            if t["error"]:
                bars += bar_row(label, [(1, "var(--critical)")], 1, t["error"])
            else:
                bars += bar_row(
                    label,
                    [(t["input_tokens"], "var(--blue)"), (t["output_tokens"], "var(--orange)")],
                    max_tokens,
                    fmt_cost(t["cost_usd"]),
                )
        chart_section = section("Tokens and cost, per turn", f"{len(turns)} turns", bars)

        rows = "".join(
            f"<tr><td>{i}</td>"
            f'<td class="mono">{(t["task"][:60] or "—")}</td>'
            f"<td>{chip(not t['error']) if t['error'] else ''}{'error: ' + t['error'] if t['error'] else 'ok'}</td>"
            f'<td class="num">{t["tool_calls"]}</td>'
            f'<td class="num">{t["input_tokens"]}/{t["output_tokens"]}</td>'
            f'<td class="num">{t["duration_ms"] / 1000:.1f}s</td>'
            f'<td class="num">{fmt_cost(t["cost_usd"])}</td></tr>'
            for i, t in enumerate(turns, start=1)
        )
        table = (
            '<div style="overflow-x:auto"><table><thead><tr>'
            "<th>#</th><th>Task</th><th>Status</th><th>Tool calls</th>"
            "<th>Tokens (in/out)</th><th>Time</th><th>Est. cost</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
        table_section = section("Turn detail", t["model"] or "", table)
        body = tiles + chart_section + table_section

    footer = (
        "Costs are estimates from <span class=\"mono\">pricing.py</span>'s point-in-time "
        "rates, not your actual invoice - see console.anthropic.com/settings/billing "
        "for that. Regenerate with <span class=\"mono\">python session_report.py</span>."
    )
    return page(
        title="Session report",
        eyebrow="coding-agent-cli · session_report.py",
        lede=(
            f'One run of <span class="mono">python main.py</span> (session '
            f'<span class="mono">{session_id}</span>), reconstructed from '
            f'<span class="mono">logs/events.jsonl</span>.'
        ),
        body_html=body,
        footer_html=footer,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="report on this specific session_id instead of the most recent one")
    parser.add_argument("--all", action="store_true", help="list every session found in the log and exit")
    args = parser.parse_args()

    events = load_events()
    sessions = list_sessions(events)

    if args.all:
        print(f"{len(sessions)} session(s) in {EVENTS_PATH}:\n")
        for sid, first_ts, turn_count in sessions:
            print(f"  {sid}   {turn_count} turn(s)")
        print("\nRe-run with --session <id> to report on one of these.")
        return

    session_id = args.session or sessions[-1][0]  # most recent by default
    if session_id not in {sid for sid, _, _ in sessions}:
        print(f'No session "{session_id}" in {EVENTS_PATH}. Run with --all to see what is there.')
        sys.exit(1)

    session_events = [e for e in events if e["session_id"] == session_id]
    turns = build_turns(session_events)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(session_id, turns), encoding="utf-8")
    print(f"Wrote {REPORT_PATH} ({len(turns)} turn(s) in session {session_id})")
    if len(sessions) > 1:
        print(f"({len(sessions) - 1} other session(s) in the log - see --all)")


if __name__ == "__main__":
    main()
