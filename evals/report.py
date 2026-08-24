"""
evals/report.py
----------------
Reads evals/history.jsonl (one line per past `run_evals.py` run) and
renders an accuracy + cost trend report - the answer to "did my last
change make the agent better or worse," which a single run's stdout table
can't tell you on its own.

Run it with:

    python evals/report.py

Needs at least one prior run of `python evals/run_evals.py` to have
something to plot.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from report_style import empty_state, legend, line_chart, page, section, stat_row  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = REPO_ROOT / "evals" / "history.jsonl"
REPORT_PATH = REPO_ROOT / "evals" / "report.html"


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        print(f"No {HISTORY_PATH} yet - run `python evals/run_evals.py` at least once first.")
        sys.exit(1)
    runs = [json.loads(line) for line in HISTORY_PATH.read_text().splitlines() if line.strip()]
    if not runs:
        print(f"{HISTORY_PATH} exists but is empty - run `python evals/run_evals.py` at least once first.")
        sys.exit(1)
    return sorted(runs, key=lambda r: r["ts"])  # oldest to newest, for the trend charts


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_cost(v) -> str:
    return f"${v:.4f}" if v is not None else "—"


def render_report(runs: list[dict]) -> str:
    latest = runs[-1]
    accuracies = [r["accuracy"] for r in runs]
    costs = [r.get("total_cost_usd") or 0.0 for r in runs]
    labels = [fmt_time(r["ts"]) for r in runs]

    delta = ""
    if len(runs) > 1:
        change = accuracies[-1] - accuracies[-2]
        delta = f"{change:+.0%} vs previous run" if change else "unchanged vs previous run"

    tiles = stat_row(
        [
            ("Latest accuracy", f"{latest['accuracy']:.0%}", delta or "first run on record"),
            ("Runs recorded", str(len(runs)), f"since {fmt_time(runs[0]['ts'])}"),
            ("Latest cost", fmt_cost(latest.get("total_cost_usd")), f"model: {latest.get('model') or 'unknown'}"),
            ("Total spent (all runs)", fmt_cost(sum(costs) or None), f"across {len(runs)} run(s)"),
        ]
    )

    if len(runs) == 1:
        trend_body = empty_state(
            "Only one run so far - the accuracy and cost <b>trend</b> charts need at "
            "least two runs to show anything. Run <code>python evals/run_evals.py</code> "
            "again (after a change) to start one."
        )
    else:
        trend_body = (
            legend([("accuracy", "var(--blue)")])
            + line_chart(accuracies, labels, color="var(--blue)", value_fmt=lambda v: f"{v:.0%}")
            + '<div style="height:18px"></div>'
            + legend([("cost per run ($)", "var(--orange)")])
            + line_chart(costs, labels, color="var(--orange)", value_fmt=lambda v: f"${v:.4f}")
        )
    trend_section = section("Accuracy and cost, run over run", f"{len(runs)} run(s)", trend_body)

    rows = "".join(
        f"<tr><td>{fmt_time(r['ts'])}</td>"
        f'<td class="mono">{r.get("model") or "—"}</td>'
        f'<td class="num">{r["passed"]}/{r["total"]} ({r["accuracy"]:.0%})</td>'
        f'<td class="num">{fmt_cost(r.get("total_cost_usd"))}</td>'
        f'<td class="num">{r.get("total_duration_ms", 0) / 1000:.1f}s</td></tr>'
        for r in reversed(runs)  # newest first in the table, oldest-first in the charts above
    )
    table = (
        '<div style="overflow-x:auto"><table><thead><tr>'
        "<th>Run</th><th>Model</th><th>Accuracy</th><th>Cost</th><th>Duration</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    table_section = section("Run history", "newest first", table)

    footer = (
        "Costs are estimates from <span class=\"mono\">pricing.py</span>'s point-in-time "
        "rates. Regenerate with <span class=\"mono\">python evals/report.py</span> any "
        "time after a new <span class=\"mono\">run_evals.py</span> run."
    )
    return page(
        title="Eval trend",
        eyebrow="coding-agent-cli · evals/report.py",
        lede=(
            "Accuracy and cost across every recorded run of "
            '<span class="mono">evals/run_evals.py</span>, read back from '
            '<span class="mono">evals/history.jsonl</span>.'
        ),
        body_html=tiles + trend_section + table_section,
        footer_html=footer,
    )


def main() -> None:
    runs = load_history()
    REPORT_PATH.write_text(render_report(runs), encoding="utf-8")
    print(f"Wrote {REPORT_PATH.relative_to(REPO_ROOT)} from {len(runs)} recorded run(s).")


if __name__ == "__main__":
    main()
