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

Run it with:

    python evals/run_evals.py

This makes real API calls and costs real money/time - it is deliberately
not wired into any CI, the same way the sibling MCP-server project in this
account treats its one live-API smoke test as manual-only.
"""

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
        # skips the interactive y/n prompt, which would otherwise block
        # forever with no one there to answer it.
        task_env = {**env, "AUTO_APPROVE_BASH": "true"}
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


def _append_history(results: list[dict]) -> None:
    """Append one line to evals/history.jsonl - this is the file
    evals/report.py reads to plot accuracy and cost across runs over time.
    Never overwritten, only appended to, so old runs stay comparable."""
    passed = sum(1 for r in results if r["passed"])
    record = {
        "ts": time.time(),
        "model": next((r["model"] for r in results if r["model"]), None),
        "tasks": results,
        "passed": passed,
        "total": len(results),
        "accuracy": passed / len(results) if results else 0.0,
        "total_input_tokens": sum(r["input_tokens"] for r in results),
        "total_output_tokens": sum(r["output_tokens"] for r in results),
        "total_cost_usd": sum(r["cost_usd"] for r in results if r["cost_usd"] is not None) or None,
        "total_duration_ms": sum(r["duration_ms"] for r in results),
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Missing ANTHROPIC_API_KEY - copy .env.example to .env and set it (see README).")
        sys.exit(1)

    print(f"Running {len(TASKS)} golden tasks against {MAIN_PY} ...\n")
    results = [run_task(task, dict(os.environ)) for task in TASKS]

    print(f"{'TASK':<18} {'RESULT':<6} {'TOOL CALLS':<11} {'TOKENS (in/out)':<17} {'COST':<9} {'TIME':<8} REASON")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        tokens = f"{r['input_tokens']}/{r['output_tokens']}"
        time_s = f"{r['duration_ms'] / 1000:.1f}s"
        print(f"{r['name']:<18} {status:<6} {r['tool_calls']:<11} {tokens:<17} {_fmt_cost(r['cost_usd']):<9} {time_s:<8} {r['reason']}")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    total_cost = sum(r["cost_usd"] for r in results if r["cost_usd"] is not None) or None
    print(f"\naccuracy: {passed}/{total} ({passed / total:.0%})   total cost: {_fmt_cost(total_cost)}")

    _append_history(results)
    print(f"\nAppended to {HISTORY_PATH.relative_to(REPO_ROOT)} - run `python evals/report.py` to see the trend across runs.")


if __name__ == "__main__":
    main()
