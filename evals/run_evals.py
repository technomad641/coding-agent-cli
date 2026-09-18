"""
evals/run_evals.py
-------------------
A small "golden task" eval harness - the closest thing this project has to
a single accuracy number for the agent.

There's no test set of (input, correct_output) pairs to check the model
against, the way you'd measure accuracy for a classifier - an agent's
output is a sequence of actions, not a label. So instead: a handful of
concrete tasks with a known-correct end state, run against the *real* CLI
(not internals we import - an actual `python main.py` subprocess, same as
a human would run), each in its own throwaway directory. "Accuracy" here
means: out of these tasks, how many ended with the filesystem in the state
we expected?

This is one useful signal, not the whole story - see the README's
"Measuring accuracy" section for the other approaches (tool-call success
rate, decline rate, LLM-as-judge) this harness does NOT implement, and why.

A single run of each task only tells you "did it work this time" -
agentic loops are non-deterministic, so one green run can't tell "reliably
solves this" apart from "got lucky once." --repeats N runs every task N
times and reports pass_count/attempts per task instead of a single
PASS/FAIL, plus an overall "how many tasks are fully reliable" accuracy -
see run_task_repeated() below.

The same reasoning applies across models, not just across attempts: an
accuracy number for one model is only interesting next to another one's.
--models runs the identical task set, with the identical checks, once per
model and prints them side by side - so "is the cheap model good enough
for this?" becomes a number instead of a hunch. See
_print_model_comparison() below for why it deliberately reports the
numbers and declines to name a winner.

Run it with:

    python evals/run_evals.py               # each task once (default)
    python evals/run_evals.py --repeats 5   # each task 5 times - 5x the cost/time of a single run
    python evals/run_evals.py --models claude-haiku-4-5,claude-sonnet-5,claude-opus-5

This makes real API calls and costs real money/time - it is deliberately
not wired into any CI, the same way the sibling MCP-server project in this
account treats its one live-API smoke test as manual-only. --repeats N
multiplies that cost by N, --models multiplies it by the number of models,
and the two multiply together - budget accordingly before raising either.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pricing import estimate_cost_usd  # noqa: E402 - needs the path insert above first

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = REPO_ROOT / "main.py"
HISTORY_PATH = REPO_ROOT / "evals" / "history.jsonl"
TASK_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# The golden tasks
# ---------------------------------------------------------------------------
# Each task is: a prompt to feed the CLI, an optional setup() to pre-create
# files before the agent runs, and a check() that inspects the resulting
# directory and returns (passed, reason).


def check_gitignore(tmp_dir: Path):
    path = tmp_dir / ".gitignore"
    if not path.exists():
        return False, ".gitignore was not created"
    content = path.read_text()
    if "__pycache__" not in content or ".env" not in content:
        return False, f".gitignore exists but is missing an expected entry: {content!r}"
    return True, "ok"


def check_bash_math(tmp_dir: Path):
    path = tmp_dir / "answer.txt"
    if not path.exists():
        return False, "answer.txt was not created"
    got = path.read_text().strip()
    if got != "391":  # 17 * 23
        return False, f"answer.txt contains {got!r}, expected '391'"
    return True, "ok"


def setup_greeting_file(tmp_dir: Path):
    (tmp_dir / "greeting.py").write_text('def greet():\n    return "Hello, world!"\n')


def check_str_replace_edit(tmp_dir: Path):
    path = tmp_dir / "greeting.py"
    if not path.exists():
        return False, "greeting.py is missing (should have been edited, not deleted)"
    content = path.read_text()
    if "Howdy, world!" not in content:
        return False, "greeting.py doesn't contain the new text"
    if "Hello, world!" in content:
        return False, "greeting.py still contains the old text"
    if "def greet" not in content:
        return False, "greeting.py lost its function definition - too much was rewritten"
    return True, "ok"


def check_multi_step(tmp_dir: Path):
    path = tmp_dir / "notes.txt"
    if not path.exists():
        return False, "notes.txt was not created"
    lines = path.read_text().splitlines()
    if lines != ["draft", "final"]:
        return False, f"notes.txt has lines {lines!r}, expected ['draft', 'final']"
    return True, "ok"


TASKS = [
    {
        "name": "gitignore",
        "prompt": (
            "Add a .gitignore file for a Python project that ignores "
            "__pycache__ directories and .env files. Just create the "
            "file - don't run any commands."
        ),
        "check": check_gitignore,
    },
    {
        "name": "bash_math",
        "prompt": (
            "Run a bash command to compute 17 * 23 and save just the "
            "numeric result to a file named answer.txt, with no extra text."
        ),
        "check": check_bash_math,
    },
    {
        "name": "str_replace_edit",
        "setup": setup_greeting_file,
        "prompt": (
            "In greeting.py, change the greeting message from "
            "'Hello, world!' to 'Howdy, world!'. Don't change anything else."
        ),
        "check": check_str_replace_edit,
    },
    {
        "name": "multi_step",
        "prompt": (
            "Create a file notes.txt containing the single line 'draft', "
            "then run a bash command to append the line 'final' to it. "
            "The file should end up with exactly two lines: draft, then final."
        ),
        "check": check_multi_step,
    },
]


# ---------------------------------------------------------------------------
# Running one task
# ---------------------------------------------------------------------------


def run_task(task: dict, env: dict) -> dict:
    """Run one task in a fresh temp directory and grade the result.

    Returns a dict with pass/fail plus whatever we could read back out of
    that run's logs/events.jsonl for the summary table below - this is the
    same event log a real interactive session produces (see
    observability.py), just read back after the subprocess exits instead
    of streamed live.
    """
    with tempfile.TemporaryDirectory(prefix="coding-agent-cli-eval-") as tmp:
        tmp_dir = Path(tmp)

        if "setup" in task:
            task["setup"](tmp_dir)

        # Run the real CLI as a subprocess, cwd pinned to the temp dir - so
        # main.py's own ROOT = Path.cwd() binds to it, exactly like a human
        # running `python main.py` from that folder would. AUTO_APPROVE_BASH
        # and AUTO_APPROVE_EDITS skip their interactive y/n prompts, which
        # would otherwise block forever with no one there to answer them -
        # any task that creates or edits a file needs the latter too, not
        # just the former (see WORKLOG.md: this harness silently failed
        # every file-creating/editing golden task for several commits after
        # the edit-approval gate shipped, because this line wasn't updated
        # to match - caught by --repeats making a *deterministic* failure
        # look exactly like maximal unreliability instead of one bad run).
        task_env = {**env, "AUTO_APPROVE_BASH": "true", "AUTO_APPROVE_EDITS": "true"}
        stdin_text = task["prompt"] + "\nexit\n"

        try:
            proc = subprocess.run(
                [sys.executable, str(MAIN_PY)],
                cwd=tmp_dir,
                env=task_env,
                input=stdin_text,
                text=True,
                capture_output=True,
                timeout=TASK_TIMEOUT_SECONDS,
            )
            crashed = proc.returncode != 0
        except subprocess.TimeoutExpired:
            crashed = True
            proc = None

        passed, reason = task["check"](tmp_dir)
        if crashed:
            passed, reason = False, "the CLI process crashed or timed out before finishing"

        metrics = _read_metrics(tmp_dir / "logs" / "events.jsonl")
        metrics["cost_usd"] = (
            estimate_cost_usd(
                metrics["model"], metrics["input_tokens"], metrics["output_tokens"], metrics["cache_read_input_tokens"]
            )
            if metrics["model"]
            else None
        )

        return {
            "name": task["name"],
            "passed": passed,
            "reason": reason,
            **metrics,
        }


def run_task_repeated(task: dict, env: dict, repeats: int) -> dict:
    """Run one task `repeats` times and summarize reliability across the
    attempts - the whole point of --repeats (see the module docstring):
    one green run can't tell "reliably solves this" apart from "got lucky
    once."

    The returned dict's field *names* are deliberately identical to a
    single run_task() call's, with their *meaning* generalized from "this
    attempt's value" to "summed across every attempt of this task" -
    `_append_history()` below already sums these across tasks to get
    run-level totals, and sum-of-sums composes the same way sum-of-values
    did, so that aggregation code needs zero changes to keep working
    whether repeats is 1 (identical to today's single-run behavior, since
    summing one value is that value) or more. The only genuinely new
    fields are `attempts`, `pass_count`, and `success_rate`.
    """
    attempts = [run_task(task, env) for _ in range(repeats)]
    pass_count = sum(1 for a in attempts if a["passed"])
    costs = [a["cost_usd"] for a in attempts if a["cost_usd"] is not None]

    if pass_count == repeats:
        reason = "ok"
    else:
        failure_reasons = sorted({a["reason"] for a in attempts if not a["passed"]})
        reason = f"flaky - {repeats - pass_count}/{repeats} failed: " + "; ".join(failure_reasons)

    return {
        "name": task["name"],
        "attempts": repeats,
        "pass_count": pass_count,
        "success_rate": pass_count / repeats,
        # "passed" stays a boolean for backward compatibility with history
        # written before --repeats existed - the strictest reading of it:
        # every single attempt passed, not just some of them.
        "passed": pass_count == repeats,
        "reason": reason,
        "model": next((a["model"] for a in attempts if a["model"]), None),
        "tool_calls": sum(a["tool_calls"] for a in attempts),
        "input_tokens": sum(a["input_tokens"] for a in attempts),
        "output_tokens": sum(a["output_tokens"] for a in attempts),
        "cache_read_input_tokens": sum(a["cache_read_input_tokens"] for a in attempts),
        "duration_ms": round(sum(a["duration_ms"] for a in attempts), 1),
        "cost_usd": sum(costs) if costs else None,
    }


def _read_metrics(events_path: Path) -> dict:
    """Pull the numbers worth reporting out of one run's event log."""
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    duration_ms = 0.0
    model = None

    if events_path.exists():
        for line in events_path.read_text().splitlines():
            record = json.loads(line)
            if record["event"] == "tool_call":
                tool_calls += 1
            elif record["event"] == "api_call":
                model = model or record.get("model")
                input_tokens += record.get("input_tokens", 0)
                output_tokens += record.get("output_tokens", 0)
                cache_read_input_tokens += record.get("cache_read_input_tokens", 0)
            elif record["event"] == "turn_end":
                duration_ms += record.get("duration_ms", 0)

    return {
        "model": model,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "duration_ms": round(duration_ms, 1),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt_cost(v) -> str:
    return f"${v:.4f}" if v is not None else "?"


def _append_history(results: list[dict], repeats: int, requested_model: str | None = None) -> None:
    """Append one line to evals/history.jsonl - this is the file
    evals/report.py reads to plot accuracy and cost across runs over time.
    Never overwritten, only appended to, so old runs stay comparable.

    `passed`/`total`/`accuracy` keep their pre---repeats meaning exactly
    when repeats=1 (a task either passed or it didn't). At repeats>1,
    `passed` means "every attempt of this task passed" (see
    run_task_repeated()) - so `accuracy` here is "fraction of tasks that
    are fully reliable," a stricter and more useful headline number than
    "fraction of all attempts that happened to pass," and it's what
    evals/report.py's existing trend chart already plots with no changes
    needed. `total_attempts`/`total_pass_count` carry the finer-grained
    pooled-attempts view for anyone reading the raw history who wants it.
    `repeats` is recorded so a reader of history.jsonl - or
    evals/report.py's run-history table - can tell a 5x-repeated run's
    accuracy apart from a single-shot one instead of silently comparing
    two different things on the same trend line.

    `requested_model` is recorded for the same reason, and answers a
    question `model` alone cannot: `model` is the model the API actually
    served, read back out of the run's own event log, while
    `requested_model` is what --models asked for. They usually match, but
    a fallback or an alias makes them differ - and when they do, a
    comparison table that grouped only on the served id would silently
    merge two arms of the experiment. It stays None for a plain run where
    nothing was requested, which is also what every history line written
    before --models existed reads as.
    """
    passed = sum(1 for r in results if r["passed"])
    record = {
        "ts": time.time(),
        "model": next((r["model"] for r in results if r["model"]), None),
        "requested_model": requested_model,
        "repeats": repeats,
        "tasks": results,
        "passed": passed,
        "total": len(results),
        "accuracy": passed / len(results) if results else 0.0,
        "total_attempts": sum(r["attempts"] for r in results),
        "total_pass_count": sum(r["pass_count"] for r in results),
        "total_input_tokens": sum(r["input_tokens"] for r in results),
        "total_output_tokens": sum(r["output_tokens"] for r in results),
        "total_cost_usd": sum(r["cost_usd"] for r in results if r["cost_usd"] is not None) or None,
        "total_duration_ms": sum(r["duration_ms"] for r in results),
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run_suite(env: dict, repeats: int) -> list[dict]:
    """Run every golden task against one model configuration, printing
    per-task progress as it goes (each attempt can take up to
    TASK_TIMEOUT_SECONDS, so a multi-repeat or multi-model run would
    otherwise look hung). Returns the per-task summaries - the same shape
    _append_history() consumes."""
    results = []
    for task in TASKS:
        if repeats > 1:
            print(f"  {task['name']}: ", end="", flush=True)
        result = run_task_repeated(task, env, repeats)
        if repeats > 1:
            print(f"{result['pass_count']}/{result['attempts']}")
        results.append(result)
    if repeats > 1:
        print()
    return results


def _print_task_table(results: list[dict], repeats: int) -> None:
    header_result_col = "RESULT" if repeats == 1 else "PASS RATE"
    print(f"{'TASK':<18} {header_result_col:<10} {'TOOL CALLS':<11} {'TOKENS (in/out)':<17} {'COST':<9} {'TIME':<8} REASON")
    for r in results:
        status = ("PASS" if r["passed"] else "FAIL") if repeats == 1 else f"{r['pass_count']}/{r['attempts']}"
        tokens = f"{r['input_tokens']}/{r['output_tokens']}"
        time_s = f"{r['duration_ms'] / 1000:.1f}s"
        print(f"{r['name']:<18} {status:<10} {r['tool_calls']:<11} {tokens:<17} {_fmt_cost(r['cost_usd']):<9} {time_s:<8} {r['reason']}")


def _print_run_summary(results: list[dict], repeats: int) -> None:
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    total_cost = sum(r["cost_usd"] for r in results if r["cost_usd"] is not None) or None
    if repeats == 1:
        print(f"\naccuracy: {passed}/{total} ({passed / total:.0%})   total cost: {_fmt_cost(total_cost)}")
    else:
        total_attempts = sum(r["attempts"] for r in results)
        total_pass_count = sum(r["pass_count"] for r in results)
        print(
            f"\n{passed}/{total} task(s) fully reliable ({passed / total:.0%}) - "
            f"{total_pass_count}/{total_attempts} attempts passed overall ({total_pass_count / total_attempts:.0%})"
            f"   total cost: {_fmt_cost(total_cost)}"
        )


def run_and_report(env: dict, repeats: int, requested_model: str | None) -> list[dict]:
    """One full pass of the task set against one model: run it, print its
    per-task table and summary line, and append its own history entry.

    Returns the per-task summaries so a caller comparing several models
    can build a side-by-side table from them - each model still gets its
    own history line either way, so evals/report.py's per-model view and
    trend keep working without knowing anything about comparison runs.
    """
    results = run_suite(env, repeats)
    _print_task_table(results, repeats)
    _print_run_summary(results, repeats)
    _append_history(results, repeats, requested_model)
    return results


def _print_model_comparison(per_model: list[tuple[str, list[dict]]], repeats: int) -> None:
    """The whole point of --models: the same task set, same checks, one
    row per model.

    Deliberately reports the numbers and stops - no "winner", no
    recommendation. Which tradeoff is right depends on what you're
    optimizing for, and 4 golden tasks is far too thin a sample to crown
    a model on anyway (see the README's "Measuring accuracy").
    """
    print("\n" + "=" * 78)
    print("MODEL COMPARISON - same tasks, same checks")
    print("=" * 78)

    accuracy_header = "ACCURACY" if repeats == 1 else "RELIABLE"
    print(f"{'MODEL':<30} {accuracy_header:<13} {'TOKENS (in/out)':<19} {'COST':<10} TIME")

    mismatches = []
    for requested, results in per_model:
        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        tokens = f"{sum(r['input_tokens'] for r in results)}/{sum(r['output_tokens'] for r in results)}"
        cost = sum(r["cost_usd"] for r in results if r["cost_usd"] is not None) or None
        time_s = f"{sum(r['duration_ms'] for r in results) / 1000:.1f}s"
        print(
            f"{requested:<30} {f'{passed}/{total} ({passed / total:.0%})':<13} "
            f"{tokens:<19} {_fmt_cost(cost):<10} {time_s}"
        )

        served = next((r["model"] for r in results if r["model"]), None)
        if served and served != requested:
            mismatches.append((requested, served))

    if repeats > 1:
        print(f"\n(RELIABLE = tasks where all {repeats} attempts passed.)")
    for requested, served in mismatches:
        print(f"(note: requested {requested}, API reported {served})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="run each task this many times and report pass_count/attempts instead of a single PASS/FAIL "
        "(default 1). Multiplies real API cost and time by roughly this factor.",
    )
    parser.add_argument(
        "--models",
        help="comma-separated model ids to run the same task set against, e.g. "
        "claude-haiku-4-5,claude-sonnet-5,claude-opus-5. Runs the whole suite once per model "
        "and prints a side-by-side comparison at the end; each model also gets its own "
        "history entry. Default: one run against whatever CLAUDE_MODEL resolves to. "
        "Multiplies real API cost and time by the number of models.",
    )
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be a positive integer")

    models = None
    if args.models is not None:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models:
            parser.error("--models needs at least one model id")

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Missing ANTHROPIC_API_KEY - copy .env.example to .env and set it (see README).")
        sys.exit(1)

    attempts_each = f" x {args.repeats} repeats each" if args.repeats > 1 else ""
    if models is None:
        total_attempts = len(TASKS) * args.repeats
        print(f"Running {len(TASKS)} golden tasks{attempts_each} ({total_attempts} attempts) against {MAIN_PY} ...\n")
        run_and_report(dict(os.environ), args.repeats, os.environ.get("CLAUDE_MODEL"))
    else:
        total_attempts = len(TASKS) * args.repeats * len(models)
        print(
            f"Running {len(TASKS)} golden tasks{attempts_each} against {len(models)} model(s) "
            f"({total_attempts} attempts total) - roughly {len(models) * args.repeats}x "
            f"the cost/time of a single run.\n"
        )
        per_model = []
        for model in models:
            print(f"--- {model} ---")
            # CLAUDE_MODEL is what main.py reads to pick its model, and
            # load_dotenv() there does not override an already-set env var,
            # so this wins over any CLAUDE_MODEL in .env.
            results = run_and_report({**os.environ, "CLAUDE_MODEL": model}, args.repeats, model)
            per_model.append((model, results))
            print()
        _print_model_comparison(per_model, args.repeats)

    print(f"\nAppended to {HISTORY_PATH.relative_to(REPO_ROOT)} - run `python evals/report.py` to see the trend across runs.")


if __name__ == "__main__":
    main()
