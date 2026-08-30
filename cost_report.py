"""
cost_report.py
---------------
Reads logs/events.jsonl across *every* `python main.py` session recorded
there - not just the most recent one, which is what session_report.py
already covers - and renders the "how much have I spent" view: total
cost, cost per day, and cost per session.

This closes the "No aggregation across sessions" gap the README's
Observability section used to just list - "$ spent this week across
every session" was a `jq`/`awk` exercise left to you; now it's a report.

Run it with:

    python cost_report.py             # every session ever logged
    python cost_report.py --days 7    # only the last 7 days
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pricing import estimate_cost_usd
from report_style import bar_row, empty_state, legend, page, section, stat_row

EVENTS_PATH = Path("logs") / "events.jsonl"
REPORT_PATH = Path("logs") / "cost_report.html"


def load_events(since_ts: float | None) -> list[dict]:
    if not EVENTS_PATH.exists():
        print(f"No {EVENTS_PATH} yet - run `python main.py`, do at least one task, then try again.")
        sys.exit(1)
    events = [json.loads(line) for line in EVENTS_PATH.read_text().splitlines() if line.strip()]
    if since_ts is not None:
        events = [e for e in events if e.get("ts", 0) >= since_ts]
    if not events:
        window = " in the requested window" if since_ts is not None else ""
        print(f"No events in {EVENTS_PATH}{window} - nothing to report. Try a wider --days, or none at all.")
        sys.exit(1)
    return events


def fmt_cost(v) -> str:
    return f"${v:.4f}" if v is not None else "—"


def fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def aggregate(events: list[dict]) -> dict:
    """Reduce every api_call event in `events` (which may span many
    sessions - this is the whole point) down to per-session and per-day
    totals, the two shapes "how much have I spent" actually gets asked in.

    Costs are summed per api_call, not computed once from summed tokens -
    matching session_report.py's build_turns() - because estimate_cost_usd()
    is only meaningful per (model, tokens) triple; a session is never
    actually split across models today (MODEL is fixed for a process's
    lifetime), but summing per-call is the version that stays correct if
    that ever changes, at no extra cost now.
    """
    sessions: dict[str, dict] = {}
    by_day: dict[str, float] = defaultdict(float)
    unpriced_calls = 0

    for e in events:
        if e["event"] != "api_call":
            continue
        sid = e["session_id"]
        s = sessions.setdefault(
            sid,
            {
                "first_ts": e["ts"],
                "model": e.get("model"),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "api_calls": 0,
                "has_unpriced": False,
            },
        )
        s["first_ts"] = min(s["first_ts"], e["ts"])
        s["api_calls"] += 1
        s["input_tokens"] += e.get("input_tokens", 0)
        s["output_tokens"] += e.get("output_tokens", 0)

        cost = estimate_cost_usd(
            e.get("model"),
            e.get("input_tokens", 0),
            e.get("output_tokens", 0),
            e.get("cache_read_input_tokens", 0),
        )
        if cost is None:
            s["has_unpriced"] = True
            unpriced_calls += 1
        else:
            s["cost"] += cost
            by_day[fmt_day(e["ts"])] += cost

    # turn_start events, not api_call events, are what actually say "one
    # turn happened" - a turn can make several api_calls (tool-calling
    # round-trips) or, rarely, none logged yet if it errored immediately.
    turn_counts: dict[str, int] = defaultdict(int)
    for e in events:
        if e["event"] == "turn_start":
            turn_counts[e["session_id"]] += 1
    for sid, s in sessions.items():
        s["turns"] = turn_counts.get(sid, 0)

    return {"sessions": sessions, "by_day": dict(by_day), "unpriced_calls": unpriced_calls}


def render_report(agg: dict, days: int | None) -> str:
    sessions = agg["sessions"]
    by_day = agg["by_day"]

    if not sessions:
        body = section(
            "Cost across sessions",
            "",
            empty_state("No api_call events yet - do at least one task with `python main.py` first."),
        )
    else:
        total_cost = sum(s["cost"] for s in sessions.values())
        total_in = sum(s["input_tokens"] for s in sessions.values())
        total_out = sum(s["output_tokens"] for s in sessions.values())
        unpriced = agg["unpriced_calls"]
        earliest = min(s["first_ts"] for s in sessions.values())

        # "—" only when literally nothing here was priced (every api_call
        # hit a model pricing.py has no rate for) - not just when the sum
        # happens to be $0.
        cost_known = total_cost > 0 or unpriced == 0
        range_sub = f"last {days} day(s)" if days else f"since {fmt_time(earliest)}"
        tiles = stat_row(
            [
                ("Total spent", fmt_cost(total_cost) if cost_known else "—", range_sub),
                ("Sessions", str(len(sessions)), f"{sum(s['turns'] for s in sessions.values())} turn(s) total"),
                ("Tokens", f"{total_in + total_out:,}", f"{total_in:,} in · {total_out:,} out"),
                ("Unpriced calls", str(unpriced), "no rate in pricing.py" if unpriced else "every call priced"),
            ]
        )

        priced_days = sorted(by_day.keys())
        if len(priced_days) < 2:
            day_body = empty_state(
                "Cost only shows up here on days with at least one priced API call - "
                "there's only one such day in this window so far."
            )
        else:
            max_day_cost = max(by_day.values())
            day_body = legend([("cost ($)", "var(--orange)")]) + "".join(
                bar_row(day, [(by_day[day], "var(--orange)")], max_day_cost, fmt_cost(by_day[day]))
                for day in priced_days
            )
        day_section = section("Cost by day", f"{len(priced_days)} day(s)", day_body)

        rows = "".join(
            f"<tr><td>{fmt_time(s['first_ts'])}</td>"
            f'<td class="mono">{sid}</td>'
            f'<td class="mono">{s["model"] or "—"}</td>'
            f'<td class="num">{s["turns"]}</td>'
            f'<td class="num">{s["input_tokens"]}/{s["output_tokens"]}</td>'
            f'<td class="num">{fmt_cost(s["cost"])}{" +" if s["has_unpriced"] else ""}</td></tr>'
            for sid, s in sorted(sessions.items(), key=lambda kv: -kv[1]["first_ts"])
        )
        table = (
            '<div style="overflow-x:auto"><table><thead><tr>'
            "<th>Started</th><th>Session</th><th>Model</th><th>Turns</th>"
            "<th>Tokens (in/out)</th><th>Cost</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
        table_section = section("Cost by session", f"{len(sessions)} session(s), newest first", table)
        body = tiles + day_section + table_section

    footer = (
        "Costs are estimates from <span class=\"mono\">pricing.py</span>'s point-in-time "
        "rates, not your actual invoice - see console.anthropic.com/settings/billing for "
        "that. A cost with a trailing <b>+</b> means at least one of that session's calls "
        "used a model pricing.py has no rate for, so the true total is higher than shown. "
        "Regenerate with <span class=\"mono\">python cost_report.py</span> (add "
        "<span class=\"mono\">--days N</span> to narrow the window)."
    )
    window_desc = f"the last {days} day(s)" if days else "every session on record"
    return page(
        title="Cost across sessions",
        eyebrow="coding-agent-cli · cost_report.py",
        lede=(
            f"Estimated spend across {window_desc}, reconstructed from "
            f'<span class="mono">logs/events.jsonl</span> - every '
            f'<span class="mono">python main.py</span> run counted, not just the latest.'
        ),
        body_html=body,
        footer_html=footer,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, help="only include events from the last N days (default: all-time)")
    args = parser.parse_args()
    if args.days is not None and args.days <= 0:
        # `if args.days` below treats 0 the same as "not given" (Python
        # truthiness) - that would make `--days 0` silently mean "all
        # time" instead of "none", which is the opposite of what someone
        # typing that flag would expect. Reject it outright instead.
        parser.error("--days must be a positive integer")

    since_ts = time.time() - args.days * 86400 if args.days else None
    events = load_events(since_ts)
    agg = aggregate(events)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(agg, args.days), encoding="utf-8")
    print(f"Wrote {REPORT_PATH} ({len(agg['sessions'])} session(s))")


if __name__ == "__main__":
    main()
