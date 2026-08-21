# Work Log

A dated log of what changed in this repo and why - the reasoning behind a
change is usually obvious in the moment and gone a week later, so it's
worth writing down separately from the commit messages. Newest entries
first. See [README.md](./README.md) for the current state of the project;
this file is the history of how it got there.

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
