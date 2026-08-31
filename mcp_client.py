"""
mcp_client.py
--------------
Makes this harness a real MCP client - it spawns the local MCP servers you
configure, lists their tools, and can call them - the same role Claude
Code and Claude Desktop play, not the Anthropic API's own server-side MCP
connector (a different, Anthropic-hosted feature for *remote* MCP servers;
see WORKLOG.md for why this project chose the local-client path instead).

Configured servers live in mcp_servers.json (gitignored - see
mcp_servers.example.json for the shape and .env.example for the same
pattern applied to secrets instead of MCP config). Each server is spawned
as a local subprocess speaking MCP over stdio - the same "stdio server"
shape Claude Desktop's own config uses, so an existing mcpServers config
mostly just works here too.

The `mcp` package (a separate library from `anthropic` - a different
protocol, a different SDK) is asyncio-only: connecting, listing tools, and
calling a tool are all coroutines. main.py's loop is - deliberately, see
its own module docstring - a plain synchronous REPL, and staying that way
matters: rewriting it to `async def` would touch every function in the
file for one feature. So this module is the bridge: a single background
thread runs its own asyncio event loop for as long as the process is
alive, and every function below is a normal blocking function that hands
work to that loop with `asyncio.run_coroutine_threadsafe(...).result()`
and waits for the answer. Nothing outside this file ever touches asyncio
directly - main.py just gets a list of tool defs and a call_tool()
function, the same shape tools.py already exposes for bash and the text
editor.

Each configured server's `mcp.Client` connection is opened once (at
connect_all()) and kept alive for the life of the process, not
reconnected per call - reconnecting per call would mean re-spawning the
server subprocess on every single tool call, which is slow and, for a
server with any per-session state, wrong. The client's async context
manager (__aenter__/__aexit__) is entered and exited manually instead of
inside one `async with` block, specifically so the connection can outlive
any single coroutine - a legitimate, if less common, use of an async
context manager. See shutdown() for how it's torn down cleanly.
"""

import asyncio
import json
import os
import threading
from pathlib import Path

import mcp

CONFIG_PATH = Path("mcp_servers.json")

# Anthropic tool names must be namespaced by server so two servers can't
# collide on a tool name (two filesystem-ish servers both offering
# "list_files", say). "__" is a safe separator - MCP tool and server names
# are conventionally identifier-like, and this keeps qualified names within
# Anthropic's tool-name character set without needing to sanitize anything.
_SEPARATOR = "__"

# One background thread, one event loop, for the life of the process.
# Every coroutine this module runs goes through this loop, from whichever
# thread happens to call the synchronous functions below (normally
# main.py's, but nothing here assumes that).
_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, daemon=True, name="mcp-client-loop")
_thread.start()

# server_name -> the live, __aenter__'d mcp.Client for that server.
# Populated by connect_all(), read by call_tool(), torn down by shutdown().
_clients: dict[str, "mcp.Client"] = {}


def _run(coro):
    """Run a coroutine on the background loop from a synchronous caller,
    and block until it's done. The one primitive everything else here is
    built from."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


def _load_config() -> dict:
    """mcp_servers.json's "mcpServers" map - server name -> {command, args,
    env?} - the same shape Claude Desktop's own config file uses. Returns
    {} (not an error) if the file doesn't exist: MCP support is opt-in: no
    config file means no MCP servers, not a broken harness.

    A malformed file also degrades to {} rather than crashing the whole
    CLI before it even starts - same "skip it, don't take down everything
    else" philosophy as session_store.list_sessions() skipping a corrupt
    session file, or a single bad server in connect_all() not stopping the
    others.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        print(f"[mcp: {CONFIG_PATH} is not valid JSON ({err}) - continuing with no MCP servers]")
        return {}
    return data.get("mcpServers", {})


def connect_all() -> list[dict]:
    """Connect to every server in mcp_servers.json and return the merged,
    Anthropic-shaped tool list (each tool's `input_schema` is exactly what
    the MCP server itself reports - already JSON Schema, same as what a
    hand-written custom Anthropic tool needs).

    Safe to call with no config file present - returns [] and main.py
    proceeds with only its two built-in tools, same as before this file
    existed. A single server that fails to start is skipped with a
    printed warning rather than aborting every other configured server or
    the whole CLI - one bad `command` in mcp_servers.json shouldn't take
    down MCP support entirely.
    """
    servers = _load_config()
    tool_defs: list[dict] = []

    for name, cfg in servers.items():
        params = mcp.StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
        )
        client = mcp.Client(params)
        try:
            _run(client.__aenter__())
        except Exception as err:  # noqa: BLE001 - one bad server shouldn't stop the others
            print(f"[mcp: could not start server \"{name}\" - {err}]")
            continue

        _clients[name] = client
        result = _run(client.list_tools())
        for tool in result.tools:
            tool_defs.append(
                {
                    "name": f"{name}{_SEPARATOR}{tool.name}",
                    "description": tool.description or "",
                    "input_schema": tool.input_schema,
                }
            )
        print(f"[mcp: connected to \"{name}\" - {len(result.tools)} tool(s)]")

    return tool_defs


def is_mcp_tool(name: str) -> bool:
    """Whether a tool_use block's name is one of ours - i.e. it came from
    connect_all()'s merged list, not the two built-in tools in tools.py.

    Requires the separator to actually be present, not just a prefix match
    - a bare name that happens to equal a connected server's name (with no
    "__tool" suffix at all) is not a qualified tool name and was never
    something connect_all() put in TOOLS, so it shouldn't route to MCP.
    """
    server_name, sep, _ = name.partition(_SEPARATOR)
    return bool(sep) and server_name in _clients


def _confirm_call(qualified_name: str, tool_input: dict) -> bool:
    """Ask the human in the loop before calling an MCP tool.

    Same gate shape as tools.py's _confirm_bash()/_confirm_edit(), kept as
    its own AUTO_APPROVE_MCP switch rather than reusing either of theirs -
    a configured MCP server is arbitrary third-party code you chose to
    run, a different trust boundary than this harness's two built-in
    tools. See the README's Threat model.
    """
    if os.environ.get("AUTO_APPROVE_MCP") == "true":
        return True
    answer = input(f"\n  call {qualified_name}({tool_input})\n  allow? [y/N] ")
    return answer.strip().lower() == "y"


def call_tool(qualified_name: str, tool_input: dict) -> str:
    """Call one MCP tool by its namespaced name and return its result as
    plain text - the same "just a string" contract tools.py's handlers
    return, so main.py's tool-result handling doesn't need an MCP-specific
    branch anywhere except dispatch itself.

    An MCP tool result can carry multiple content blocks (text, image,
    embedded resource, ...) - only text blocks are rendered today; a
    non-text block becomes a placeholder line rather than being silently
    dropped, so a tool that returns e.g. an image doesn't just look like
    it returned nothing.
    """
    server_name, _, tool_name = qualified_name.partition(_SEPARATOR)
    client = _clients.get(server_name)
    if client is None:
        return f'Error: no connected MCP server named "{server_name}"'

    if not _confirm_call(qualified_name, tool_input):
        return "MCP call declined by the user - not run."

    try:
        result = _run(client.call_tool(tool_name, tool_input))
    except Exception as err:  # noqa: BLE001 - becomes a tool_result Claude can react to
        return f"Error: MCP call to {qualified_name} failed - {err}"

    lines = []
    for block in result.content:
        if block.type == "text":
            lines.append(block.text)
        else:
            lines.append(f"[non-text MCP content: {block.type}]")
    text = "\n".join(lines) or "(no content)"
    return f"Error: {text}" if result.is_error else text


def shutdown() -> None:
    """Cleanly close every connected server's session, then stop the
    background loop. Called once, at process exit (see main.py) - safe to
    call even if connect_all() was never called (nothing to close)."""
    for client in _clients.values():
        try:
            _run(client.__aexit__(None, None, None))
        except Exception:  # noqa: BLE001 - best-effort cleanup, not worth crashing exit over
            pass
    _clients.clear()
    _loop.call_soon_threadsafe(_loop.stop)
