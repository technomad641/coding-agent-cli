# Work Log

A dated log of what changed in this repo and why - the reasoning behind a
change is usually obvious in the moment and gone a week later, so it's
worth writing down separately from the commit messages. Newest entries
first. See [README.md](./README.md) for the current state of the project;
this file is the history of how it got there.

## 2026-08-24 - Budget guardrail

- `main.py` now tallies `pricing.estimate_cost_usd(...)` after every API
  response and raises internally the moment the running session total
  reaches `SESSION_BUDGET_USD` (default `1.00`, `0` disables it) -
  checked after every call, not just between turns, so one long
  tool-calling turn can't blow past the cap unnoticed. Hitting it ends
  the whole session, not just the current task: returning to the prompt
  instead would leave `messages` with a tool_use and no matching
  tool_result, which the next API call would reject outright, and
  there's no persistence yet to safely resume from anyway.
- Logged as an `error` event with `kind: "budget_exceeded"` - reused the
  existing event/kind pattern instead of inventing a new event type, so
  `session_report.py` needed zero changes to render it correctly (shows
  as a FAIL with a red bar, same as any other failed turn).
- Startup banner now prints the configured budget, so it's never a silent
  surprise: `session budget: $1.00 (SESSION_BUDGET_USD in .env - 0
  disables it)`.
- If `CLAUDE_MODEL` points at a model `pricing.py` has no rate for, the
  guardrail can't track cost for those calls - warns once, at the first
  such call, instead of silently doing nothing.
- **Verified for real**, not just against a dummy key: set
  `SESSION_BUDGET_USD=0.005` in a throwaway directory and confirmed it
  tripped after exactly one API call, before the pending tool call ran
  (`one.txt` was never created), logged correctly, ended the session
  outright (a second queued task in stdin was never attempted), and
  rendered correctly in `session_report.py` - screenshotted to confirm,
  same as prior report work.
- **Found a real, separate, unfixed bug while keeping that test cheap**:
  tried `CLAUDE_MODEL=claude-haiku-4-5` to use a cheaper model for the
  trip test, and every call failed with `adaptive thinking is not
  supported on this model`. `main.py` hardcodes
  `thinking={"type": "adaptive"}` for every request; Haiku-tier models
  don't support it. Out of scope for today's task, so not fixed here -
  documented in the Configuration table and Troubleshooting instead of
  silently worked around, since going quiet about a bug found by accident
  isn't the same as it not existing.

## 2026-08-24 - First real run against a live key

- A funded `ANTHROPIC_API_KEY` became available for the first time. Ran
  the pipeline for real instead of against synthetic data or a dummy key:
  - `python main.py` with a real task ("add a .gitignore for a python
    project that ignores `__pycache__` and `.env`") - completed correctly
    in one tool call, 1,776+163 then 1,963+58 tokens across two `api_call`
    events, ~$0.024, ~3.8s. `session_report.py` rendered it correctly from
    the real `logs/events.jsonl`.
  - `python evals/run_evals.py` - **4/4 (100%), $0.1369 total**, the golden
    tasks' first real accuracy number. `str_replace_edit` took 3 tool
    calls and `multi_step` took 2 (both passed anyway) - worth watching
    whether that call count holds steady or grows on future runs.
  - `python evals/report.py` - renders correctly with one real history
    row; the "needs 2+ runs" empty state confirmed working as intended
    rather than showing a broken chart.
- **A real mistake, caught and fixed immediately:** the first attempt ran
  `python main.py` directly inside this repo's own checkout instead of a
  throwaway directory - exactly the scenario the README's Usage section
  warns about. The agent did exactly what it was asked and overwrote this
  repo's actual `.gitignore` with a project-specific one (`create` backs
  up to `.gitignore.bak`, so nothing was destructively lost, but the
  original was gone from the working file). Caught via `git diff --stat`,
  fixed with `git restore .gitignore`, `.bak` removed, verified clean
  before continuing - re-ran the same task in `/tmp/real-agent-test`
  instead. Left in this log rather than quietly fixed, because it's a
  real demonstration of exactly the risk the Threat model section already
  described in the abstract.
- The API key used for this was pasted directly into a chat message
  rather than set via an environment variable or a locally-created
  `.env` - written straight to a local, gitignored `.env` (confirmed with
  `git check-ignore -v .env` before writing anything), never echoed back,
  never committed. Recommended rotating it at
  console.anthropic.com/settings/keys after this session regardless,
  since a key typed into a chat transcript should be treated as exposed
  even when nothing further goes wrong with it.

## 2026-08-24 - Cost/accuracy trend and per-session cost reports

- Added `session_id` to `observability.py`: one per `python main.py`
  process, generated at import time and stamped on every event, so a
  "session" in a report means one run - not the whole, ever-growing
  `logs/events.jsonl` file.
- Added [`pricing.py`](./pricing.py): a small, explicitly point-in-time
  $/token rate table, used to turn logged token counts into an estimated
  dollar figure. Returns `None` (not `$0.00`) for a model it has no
  pricing for, so an unpriced call is visibly "unknown" in a report
  instead of silently counted as free.
- Added [`report_style.py`](./report_style.py): the shared HTML/CSS shell,
  stat tiles, bar rows, and a small inline-SVG line-chart generator used
  by both reports below - factored out so they read as one system, not
  two different tools, and so a palette change happens in one place.
- Added [`session_report.py`](./session_report.py): reads
  `logs/events.jsonl`, groups it by `session_id`, and writes
  `logs/session_report.html` - stat tiles, a per-turn token/cost bar
  chart, and a detail table. `--all` lists every session in the log;
  `--session <id>` reports on a specific one instead of the latest.
- `evals/run_evals.py` now estimates a cost per task (via `pricing.py`)
  and appends one line per run to `evals/history.jsonl` - never
  overwritten, so runs stay comparable across changes.
- Added [`evals/report.py`](./evals/report.py): reads
  `evals/history.jsonl` and writes `evals/report.html` - accuracy and
  cost line charts across every recorded run, plus a run history table.
  Handles zero and one recorded run gracefully instead of drawing a
  broken or empty chart.
- Fixed a real bug caught while testing with a dummy key: `run_evals.py`'s
  summary line printed `total cost: $0.0000` when no task had a priced
  cost, instead of `?` - conflating "genuinely free" with "unknown, no
  priced calls happened." Same `sum(...) or None` fix already used
  elsewhere in the file.
- Verified: `pricing.py`'s math by hand (with and without cache-read
  tokens, and an unknown model); both report scripts against synthetic
  data shaped exactly like the real schemas (two sessions including an
  error turn; three eval history rows; the zero-run and one-run edge
  cases) with the generated HTML actually screenshotted via a headless
  browser to confirm the charts render correctly, not just that the
  script exits 0; the full real pipeline (`main.py` -> `session_report.py`
  and `run_evals.py` -> `evals/report.py`) end-to-end against the real
  code under a dummy key.
- README: Observability section documents `session_id` and
  `session_report.py`; Measuring accuracy documents `evals/history.jsonl`
  and `evals/report.py`, and a stale "no cost aggregation" claim was
  corrected instead of left to rot; project layout updated.

## 2026-08-21 - Observability and an accuracy eval harness

- Added [`observability.py`](./observability.py): a dependency-free
  structured event logger. Every turn now writes `turn_start` / `api_call`
  / `tool_call` / `turn_end` / `error` events to `logs/events.jsonl`
  (gitignored - it's a runtime artifact), all tagged with a per-turn
  `trace_id` so one turn's events can be pulled out of the flat file with
  `grep`.
- Wired it into `main.py`: token usage (`input_tokens`, `output_tokens`,
  `cache_read_input_tokens`) and latency per API call; duration and a
  heuristic success flag per tool call; tool-call count and total duration
  per turn.
- Added [`evals/run_evals.py`](./evals/run_evals.py): 4 golden tasks
  (create a `.gitignore`, do arithmetic via `bash`, edit a file with
  `str_replace`, a multi-tool task chaining a file create with a `bash`
  append). Each runs as a real `python main.py` subprocess in its own
  throwaway temp directory with `AUTO_APPROVE_BASH=true`, graded against
  an exact expected end state, with tool-call count/tokens/duration read
  back from that run's own `logs/events.jsonl` for the report. "Accuracy"
  = pass count / total.
- Verified: the observability wiring end-to-end (turn_start/error events
  fire, are valid JSONL, share a trace_id) and the eval harness's full
  subprocess/temp-dir/grading mechanics (all 4 tasks reported the correct
  failure reason under a dummy API key - `setup()` hooks and `check()`
  functions all exercised). Did not have a funded API key in this
  environment to verify real pass rates or the `api_call`/`tool_call`
  logging paths against a live model - noted as such rather than assumed.
- README: new [Observability](./README.md#observability) and
  [Measuring accuracy](./README.md#measuring-accuracy) sections; a `Logs`
  node added to the architecture diagram (dotted edge - it's a
  side-channel, not part of the main request loop); a new Threat model
  bullet noting tool output now persists to disk in `logs/`; the "no test
  suite" limitation corrected to describe what `evals/run_evals.py`
  actually covers; this file created.

## 2026-08-21 - Rewrite in Python

- Replaced the TypeScript implementation entirely with `main.py` +
  `tools.py`, heavily commented for readability. Same two-file split as
  before, same two Anthropic-defined tools, same safety model (y/n
  approval gate on `bash`, path confinement on the editor tool).
- `resolve_within_root()`'s symlink handling got simpler in the port:
  `Path.resolve()` + `.is_relative_to()` replaces the manual
  walk-up-and-realpath loop the TypeScript version needed - one stdlib
  call doing what used to take several.
- Verified the port before touching the live API: exercised `tools.py`'s
  functions directly (a path-traversal attempt, a real symlink escape,
  all four text-editor commands, the bash tool with `AUTO_APPROVE_BASH`),
  then a startup smoke test with a dummy key.
- Updated the README throughout - both diagrams' node labels and function
  names, the loop code snippet, Setup/Usage/Project layout, the Threat
  model's description of the (simpler) symlink handling - and regenerated
  `docs/demo.gif` so its first frame shows `$ python main.py` instead of
  the now-wrong `$ npm start`.

## 2026-08-19 - Demo GIF, bullet-point cleanups, upgraded diagrams

- Added `docs/demo.gif`: a hand-rendered terminal session matching the
  README's Usage transcript exactly - not a screen recording, drawn
  frame-by-frame by `scripts/make_demo_gif.py` (Pillow), committed
  alongside the GIF so it's reproducible.
- Expanded the Tools section from one shared paragraph into an explicit
  what/why breakdown per tool, and reformatted the Threat model from
  paragraph-style into flat bullets throughout.
- Reformatted "Why this exists" into bullet points as well.
- Upgraded the architecture diagram: grouped nodes into "your machine" vs.
  "Anthropic's servers" trust-boundary subgraphs (mirroring the Threat
  model split), added shape semantics (cylinders for external resources,
  rectangles for code, one diamond for the single decision point),
  highlighted the two guarded functions, and added a sequence diagram
  showing one full turn over time, including the bash approval round-trip
  as an `opt` block inside the main `loop`. Confirmed GitHub's Mermaid
  `click` links are currently broken (via search, not assumed) before
  deciding not to use them; validated both diagrams with `mermaid.parse()`
  and a local headless render before committing.

## 2026-08-18 - Project start: the TypeScript harness

- Scaffolded the original version in TypeScript: `src/index.ts` (REPL +
  manual agentic loop) and `src/tools.ts` (bash + text-editor tool
  handlers, with path confinement enforced via a manual symlink-resolving
  walk-up loop).
- Wrote the first README pass, then expanded it the same day into the
  fuller version: an architecture diagram (Mermaid flowchart), a code
  walkthrough of the loop, a threat model, and a comparison table against
  the Tool Runner, the Claude Agent SDK, and Managed Agents - establishing
  the "write it yourself, understand every line" framing the whole project
  has kept since.
