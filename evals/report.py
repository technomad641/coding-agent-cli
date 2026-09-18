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
from report_style import bar_row, empty_state, legend, line_chart, page, section, stat_row  # noqa: E402

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


def run_model(run: dict) -> str:
    """Which model a history line belongs to, for grouping.

    Prefers `requested_model` (what `--models` asked for) over `model`
    (what the API reported serving). They usually match, but when they
    don't - a fallback, or an alias resolving to a dated snapshot id -
    grouping on the served id would either merge two arms of a comparison
    or split one model across two rows. History lines written before
    --models existed have no requested_model, so they fall through to the
    served id, which is the only thing they ever recorded.
    """
    return run.get("requested_model") or run.get("model") or "unknown"


def latest_run_per_model(runs: list[dict]) -> list[dict]:
    """The most recent run for each model, oldest model first.

    Deliberately the *latest* run rather than an average across runs: two
    models are only comparable when they ran the same task set against
    the same agent code, and runs from different days generally didn't.
    """
    by_model: dict[str, dict] = {}
    for run in runs:  # history is append-only and chronological, so last write wins
        by_model[run_model(run)] = run
    return list(by_model.values())


def render_model_comparison(runs: list[dict]) -> str:
    """A side-by-side accuracy/cost view, shown only once history actually
    contains more than one model.

    This exists because the trend charts above become misleading the
    moment `run_evals.py --models` writes several models into one history
    file: a line that dips between two points looks like a regression
    when it may just be a cheaper model's turn. Same honesty problem the
    Repeats column already solves, same fix - separate the things that
    aren't comparable instead of averaging them.
    """
    latest = latest_run_per_model(runs)
    if len(latest) < 2:
        return ""

    max_cost = max((r.get("total_cost_usd") or 0.0) for r in latest)
    accuracy_bars = "".join(
        bar_row(run_model(r), [(r["accuracy"], "var(--blue)")], 1.0,
                f"{r['passed']}/{r['total']} ({r['accuracy']:.0%})")
        for r in latest
    )
    cost_bars = "".join(
        bar_row(run_model(r), [(r.get("total_cost_usd") or 0.0, "var(--orange)")], max_cost,
                fmt_cost(r.get("total_cost_usd")))
        for r in latest
    )

    repeat_counts = {r.get("repeats", 1) for r in latest}
    caveat = (
        '<p style="font-size:12px;color:var(--ink-muted);margin:10px 0 0">'
        "Each bar is that model&rsquo;s <b>most recent</b> run, not an average - runs "
        "from different days may have been measured against different agent code. "
        + ("Repeat counts differ across these runs, so their accuracy numbers don&rsquo;t "
           "mean the same thing; see the Run history note below. " if len(repeat_counts) > 1 else "")
        + "These are a handful of tasks, not a benchmark: read the numbers, don&rsquo;t "
        "crown a winner.</p>"
    )

    return section(
        "Model comparison",
        f"{len(latest)} model(s), latest run each",
        legend([("accuracy", "var(--blue)")]) + accuracy_bars
        + '<div style="height:18px"></div>'
        + legend([("cost per run ($)", "var(--orange)")]) + cost_bars
        + caveat,
    )


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
            ("Latest cost", fmt_cost(latest.get("total_cost_usd")), f"model: {run_model(latest)}"),
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
    if len({run_model(r) for r in runs}) > 1:
        # Without this, a dip in the line reads as a regression when it
        # may only be a cheaper model's turn in a --models run. The
        # per-model breakdown below is the chart to read instead.
        trend_body += (
            '<p style="font-size:12px;color:var(--ink-muted);margin:10px 0 0">'
            "These runs span <b>more than one model</b>, so this is a timeline, not a "
            "like-for-like trend - a drop may just be a different model&rsquo;s turn. "
            "See <b>Model comparison</b> below for the per-model view.</p>"
        )
    trend_section = section("Accuracy and cost, run over run", f"{len(runs)} run(s)", trend_body)

    rows = "".join(
        f"<tr><td>{fmt_time(r['ts'])}</td>"
        f'<td class="mono">{run_model(r)}</td>'
        f'<td class="num">{r.get("repeats", 1)}x</td>'
        f'<td class="num">{r["passed"]}/{r["total"]} ({r["accuracy"]:.0%})</td>'
        f'<td class="num">{fmt_cost(r.get("total_cost_usd"))}</td>'
        f'<td class="num">{r.get("total_duration_ms", 0) / 1000:.1f}s</td></tr>'
        for r in reversed(runs)  # newest first in the table, oldest-first in the charts above
    )
    table = (
        '<div style="overflow-x:auto"><table><thead><tr>'
        "<th>Run</th><th>Model</th><th>Repeats</th><th>Accuracy</th><th>Cost</th><th>Duration</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    table_section = section(
        "Run history",
        "newest first",
        table
        + (
            '<p style="font-size:12px;color:var(--ink-muted);margin:10px 0 0">'
            "Accuracy at <b>1x</b> means each task ran once - a pass either happened or "
            'didn’t. At <b>N&gt;1x</b>, "Accuracy" means the fraction of tasks that '
            "passed <i>every</i> repeat (see <span class=\"mono\">evals/run_evals.py "
            "--repeats</span>) - a stricter number than at 1x, so don't compare accuracy "
            "across rows with different repeat counts as if they measured the same thing."
            "</p>"
            if any(r.get("repeats", 1) != 1 for r in runs)
            else ""
        ),
    )

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
        body_html=tiles + trend_section + render_model_comparison(runs) + table_section,
        footer_html=footer,
    )


def main() -> None:
    runs = load_history()
    REPORT_PATH.write_text(render_report(runs), encoding="utf-8")
    print(f"Wrote {REPORT_PATH.relative_to(REPO_ROOT)} from {len(runs)} recorded run(s).")


if __name__ == "__main__":
    main()
