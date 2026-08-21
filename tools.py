"""
tools.py
--------
Everything in this file is what actually touches your machine: running
shell commands, and reading/writing files. Claude never runs these
functions directly - all it can do is send back a "tool_use" block asking
for one of them by name. It's main.py's loop that decides to actually call
these functions and hands the result back to Claude.

Two tools live here, matching the two Anthropic-defined built-in tools
this harness uses (see the README's "Tools this harness supports"
section for the full "what and why"):

    "bash"                        -> handle_bash()
    "str_replace_based_edit_tool" -> handle_text_editor()

Both are Anthropic-defined tools, which means Claude already knows their
input shape from training - we don't write a JSON schema for them, we
just declare {"type": ..., "name": ...} and handle whatever input arrives.
"""

import os
import shutil
import subprocess
from pathlib import Path

# The tool *definitions* sent to the API on every request. This is the
# entire "contract" Claude sees - just a type and a name, no schema.
TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
]

# Cap how much combined stdout+stderr text a single bash command can return,
# so one runaway command (e.g. `cat` on a huge file) can't blow up memory
# or flood the conversation with tokens.
MAX_BASH_OUTPUT_CHARS = 10_000_000


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------
# Claude's `path` input is untrusted - it's just text a language model
# generated, and nothing upstream checks it for us. Before the text-editor
# tool touches the filesystem, every path goes through resolve_within_root(),
# which turns it into a real, absolute path and refuses anything that lands
# outside `root` (the folder the CLI was launched from). That covers both
# ".." style traversal and a symlink planted inside root that points
# somewhere else - Path.resolve() follows symlinks for every path segment
# that already exists, so there's no separate symlink check needed.


def resolve_within_root(root: Path, raw_path: str) -> Path:
    """Turn a model-supplied path into a real path - or raise if it escapes root."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    # resolve() normalizes ".." segments and follows any symlinks along the
    # way; strict=False means it won't error just because the file doesn't
    # exist yet (needed for the "create" command, which makes new files).
    candidate = candidate.resolve(strict=False)

    if not candidate.is_relative_to(root):
        raise ValueError(
            f'Refusing to touch "{raw_path}" - it resolves outside the project root ({root}).'
        )
    return candidate


# ---------------------------------------------------------------------------
# The bash tool
# ---------------------------------------------------------------------------


def handle_bash(tool_input: dict, root: Path) -> str:
    """Run one shell command, after asking the human to approve it first."""
    # The bash tool can also send {"restart": true} to reset a persistent
    # shell session. This harness doesn't keep one (every command runs
    # fresh via subprocess.run), so there's nothing to actually restart -
    # we just acknowledge it so Claude doesn't get a confusing error back.
    if tool_input.get("restart") is True:
        return "(nothing to restart - this harness runs each command fresh, no persistent shell session)"

    command = str(tool_input.get("command", "")).strip()
    if not command:
        return "Error: empty command"

    if not _confirm_bash(command):
        return "Command declined by the user - not run."

    try:
        result = subprocess.run(
            command,
            shell=True,  # lets Claude use pipes, &&, etc. - see the README's Threat model
            cwd=root,
            capture_output=True,
            text=True,  # decode stdout/stderr as text instead of bytes
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120s"

    # Combined stdout+stderr comes back either way, success or failure - a
    # non-zero exit code isn't an exception the loop has to catch, it's just
    # text Claude reads and reacts to, the same way you'd read a red
    # terminal.
    combined = (result.stdout + result.stderr).strip()
    if len(combined) > MAX_BASH_OUTPUT_CHARS:
        combined = combined[:MAX_BASH_OUTPUT_CHARS] + "\n... (truncated)"
    return combined or "(no output)"


def _confirm_bash(command: str) -> bool:
    """Ask the human in the loop before running anything.

    This prompt IS the actual safety control for the bash tool - there's no
    command allowlist (see the README's Threat model for why). Setting
    AUTO_APPROVE_BASH=true in .env skips this entirely; only do that in a
    directory you'd hand unattended shell access to.
    """
    if os.environ.get("AUTO_APPROVE_BASH") == "true":
        return True
    answer = input(f"\n  run: {command}\n  allow? [y/N] ")
    return answer.strip().lower() == "y"


# ---------------------------------------------------------------------------
# The text-editor tool: view / create / str_replace / insert
# ---------------------------------------------------------------------------


def handle_text_editor(tool_input: dict, root: Path) -> str:
    """Dispatch to the right file operation based on tool_input["command"]."""
    command = tool_input.get("command", "")
    raw_path = str(tool_input.get("path", ""))

    try:
        # Every branch below goes through this check first - there's no
        # code path in this file that touches a raw, unchecked path.
        path = resolve_within_root(root, raw_path)

        if command == "view":
            return _view(path, tool_input.get("view_range"))
        if command == "create":
            return _create(path, tool_input.get("file_text", ""), raw_path)
        if command == "str_replace":
            return _str_replace(
                path, tool_input.get("old_str", ""), tool_input.get("new_str", ""), raw_path
            )
        if command == "insert":
            return _insert(
                path, int(tool_input.get("insert_line", 0)), tool_input.get("insert_text", ""), raw_path
            )
        return f'Error: unknown text editor command "{command}"'
    except Exception as err:  # noqa: BLE001 - deliberately broad: any failure
        # becomes a tool_result Claude can read and react to, instead of
        # crashing the whole CLI over one bad file operation.
        return f"Error: {err}"


def _view(path: Path, view_range: list | None) -> str:
    """Return a file's contents, numbered like `cat -n`, optionally sliced
    to a specific 1-indexed, inclusive line range."""
    lines = path.read_text(encoding="utf-8").split("\n")
    start, end = (view_range[0], view_range[1]) if view_range else (1, len(lines))
    numbered = [f"{start + i}\t{line}" for i, line in enumerate(lines[start - 1 : end])]
    return "\n".join(numbered)


def _create(path: Path, file_text: str, raw_path: str) -> str:
    """Write a new file (or overwrite one, after backing it up to .bak)."""
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(file_text, encoding="utf-8")
    return f"Created {raw_path}"


def _str_replace(path: Path, old_str: str, new_str: str, raw_path: str) -> str:
    """Replace exactly one occurrence of old_str with new_str.

    Deliberately refuses to guess: if old_str appears zero times or more
    than once, this returns an error instead of picking an occurrence for
    Claude - a smaller, more reviewable change than "here's a new file,
    trust me".
    """
    content = path.read_text(encoding="utf-8")
    occurrences = content.count(old_str)
    if occurrences == 0:
        return f"Error: old_str not found in {raw_path}"
    if occurrences > 1:
        return f"Error: old_str matches {occurrences} times in {raw_path} - must match exactly once"
    path.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return f"Edited {raw_path}"


def _insert(path: Path, insert_line: int, insert_text: str, raw_path: str) -> str:
    """Insert a line of text after line number `insert_line` (0 = top of file)."""
    lines = path.read_text(encoding="utf-8").split("\n")
    lines.insert(insert_line, insert_text)
    path.write_text("\n".join(lines), encoding="utf-8")
    return f"Inserted into {raw_path} after line {insert_line}"
