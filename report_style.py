"""
report_style.py
----------------
Shared HTML/CSS shell and small chart helpers for the generated reports
in this repo:

    evals/report.py    - accuracy and cost trend across eval runs
    session_report.py  - per-turn token/cost breakdown for one session
    cost_report.py     - cost across every session, not just one

Factored out so all of them look like one system instead of several
different tools, and so a palette/layout change happens in one place.
Everything here returns plain strings - these functions build up an HTML
document by string concatenation, no templating engine, so the whole
pipeline stays readable top to bottom.
"""

from html import escape

PAGE_CSS = """
  :root {
    --page: #f9f9f7; --surface: #fcfcfb; --surface-2: #f3f2ee;
    --ink: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
    --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
    --blue: #2a78d6; --orange: #eb6834;
    --good: #0ca30c; --good-bg: #e6f6e6;
    --critical: #d03b3b; --critical-bg: #fbeaea;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232322;
      --ink: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
      --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
      --blue: #3987e5; --orange: #d95926;
      --good: #0ca30c; --good-bg: #123312;
      --critical: #e66767; --critical-bg: #3a1616;
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232322;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --blue: #3987e5; --orange: #d95926;
    --good: #0ca30c; --good-bg: #123312;
    --critical: #e66767; --critical-bg: #3a1616;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace; }
  main { max-width: 980px; margin: 0 auto; padding: 40px 24px 64px; display: flex; flex-direction: column; gap: 28px; }
  header { display: flex; flex-direction: column; gap: 8px; }
  .eyebrow { font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-muted); }
  h1 { font-size: 24px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
  .lede { font-size: 13.5px; color: var(--ink-2); max-width: 68ch; line-height: 1.55; margin: 0; }
  .stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .stat-tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; display: flex; flex-direction: column; gap: 6px; }
  .stat-label { font-size: 11.5px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-muted); }
  .stat-value { font-size: 25px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
  .stat-sub { font-size: 12px; color: var(--ink-2); }
  section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px 22px 22px; }
  .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  .section-title { font-size: 14.5px; font-weight: 700; }
  .section-note { font-size: 12px; color: var(--ink-muted); }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2); margin-bottom: 10px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-muted); padding: 0 10px 8px; border-bottom: 1px solid var(--grid); }
  td { padding: 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  td.num { font-variant-numeric: tabular-nums; color: var(--ink-2); }
  .chip { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 700; letter-spacing: 0.02em; padding: 3px 9px; border-radius: 100px; }
  .chip.pass { background: var(--good-bg); color: var(--good); }
  .chip.fail { background: var(--critical-bg); color: var(--critical); }
  .chip .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .bar-row { display: grid; grid-template-columns: 130px 1fr 60px; align-items: center; gap: 10px; margin-bottom: 8px; }
  .bar-label { font-size: 12px; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-track { display: flex; height: 12px; background: var(--surface-2); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; }
  .bar-value { font-size: 12px; text-align: right; font-variant-numeric: tabular-nums; color: var(--ink-2); }
  .empty { color: var(--ink-2); font-size: 13.5px; line-height: 1.6; padding: 8px 0; }
  .empty code { background: var(--surface-2); padding: 1px 6px; border-radius: 4px; }
  footer { font-size: 12px; color: var(--ink-muted); }
  @media (max-width: 760px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
"""


def page(title: str, eyebrow: str, lede: str, body_html: str, footer_html: str = "") -> str:
    """Wrap a body of section HTML in the shared page shell."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">{escape(eyebrow)}</div>
    <h1>{escape(title)}</h1>
    <p class="lede">{lede}</p>
  </header>
  {body_html}
  <footer>{footer_html}</footer>
</main>
</body>
</html>
"""


def stat_row(tiles: list[tuple[str, str, str]]) -> str:
    """`tiles` is a list of (label, value, sub) triples."""
    cells = "".join(
        f'<div class="stat-tile"><div class="stat-label">{escape(label)}</div>'
        f'<div class="stat-value">{escape(str(value))}</div>'
        f'<div class="stat-sub">{escape(sub)}</div></div>'
        for label, value, sub in tiles
    )
    return f'<div class="stat-row">{cells}</div>'


def section(title: str, note: str, body_html: str) -> str:
    return (
        f'<section><div class="section-head">'
        f'<div class="section-title">{escape(title)}</div>'
        f'<div class="section-note">{escape(note)}</div></div>{body_html}</section>'
    )


def legend(items: list[tuple[str, str]]) -> str:
    """`items` is a list of (label, css-color-value) pairs, e.g. ("input tokens", "var(--blue)")."""
    spans = "".join(
        f'<span><i class="swatch" style="background:{color}"></i>{escape(label)}</span>'
        for label, color in items
    )
    return f'<div class="legend">{spans}</div>'


def chip(passed: bool) -> str:
    return f'<span class="chip {"pass" if passed else "fail"}"><i class="dot"></i>{"PASS" if passed else "FAIL"}</span>'


def bar_row(label: str, segments: list[tuple[float, str]], max_value: float, value_label: str) -> str:
    """One labeled horizontal bar, optionally split into colored segments.

    `segments` is a list of (value, css-color) pairs, drawn left to right
    and scaled against `max_value` - pass a single segment for a plain bar.
    """
    width_pct = lambda v: f"{(v / max_value * 100 if max_value else 0):.1f}%"
    fills = "".join(f'<div class="bar-fill" style="width:{width_pct(v)}; background:{color}"></div>' for v, color in segments)
    return (
        f'<div class="bar-row"><div class="bar-label" title="{escape(label)}">{escape(label)}</div>'
        f'<div class="bar-track">{fills}</div>'
        f'<div class="bar-value">{escape(value_label)}</div></div>'
    )


def empty_state(message_html: str) -> str:
    return f'<div class="empty">{message_html}</div>'


# ---------------------------------------------------------------------------
# A small inline-SVG line chart, for trends over multiple runs.
# ---------------------------------------------------------------------------


def line_chart(
    values: list[float],
    point_labels: list[str],
    color: str = "var(--blue)",
    value_fmt=lambda v: f"{v:.2f}",
    height: int = 140,
) -> str:
    """Render `values` (oldest to newest) as a line chart with a dot per
    point and a native SVG tooltip on hover. Handles the n=1 case (a flat
    line, since there's nothing to compare yet) without special-casing the
    caller."""
    n = len(values)
    pad = 30
    width = max(360, 70 * n)
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin, vmax = vmin - 1, vmax + 1  # avoid a division by zero for a flat/one-point series

    def x(i: int) -> float:
        return pad + (i / max(n - 1, 1)) * (width - 2 * pad)

    def y(v: float) -> float:
        return height - pad - (v - vmin) / (vmax - vmin) * (height - 2 * pad)

    points_attr = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="1.5">'
        f"<title>{escape(point_labels[i])}: {value_fmt(v)}</title></circle>"
        for i, v in enumerate(values)
    )
    gridline_y = height - pad
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" aria-label="trend chart">'
        f'<line x1="{pad}" y1="{gridline_y:.1f}" x2="{width - pad}" y2="{gridline_y:.1f}" stroke="var(--grid)" stroke-width="1"/>'
        f'<polyline points="{points_attr}" fill="none" stroke="{color}" stroke-width="2"/>'
        f"{dots}"
        f"</svg>"
    )
