"""
tests/test_mcp_client.py
--------------------------
Unit tests for mcp_client.py, in isolation - config loading, tool
namespacing/dispatch, and the approval gate, all with the real `mcp.Client`
mocked out so these stay fast and never spawn a real subprocess.

A real, live round trip (a genuine spawned MCP server, a real model
picking a real MCP tool, a real approval prompt) is exactly what unit
tests can't stand in for - see WORKLOG.md for that verification. These
tests are the fast, free, everyday-regression layer underneath it, the
same relationship tests/test_tools.py has to evals/run_evals.py.

Note: importing mcp_client starts one background daemon thread (its
asyncio event loop) as a side effect of import - harmless for a test
process (no argv parsing, no external client construction, matching the
same "acceptable side effect" bar tools.py and cost_report.py already
clear), unlike main.py's import-time side effects, which is why main.py
still has no unit-test file.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mcp_client


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config_path = mcp_client.CONFIG_PATH
        mcp_client.CONFIG_PATH = Path(self._tmpdir.name) / "mcp_servers.json"

    def tearDown(self):
        mcp_client.CONFIG_PATH = self._orig_config_path
        self._tmpdir.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(mcp_client._load_config(), {})

    def test_malformed_json_degrades_to_empty_instead_of_raising(self):
        mcp_client.CONFIG_PATH.write_text("{not valid json")
        self.assertEqual(mcp_client._load_config(), {})

    def test_valid_config_returns_mcp_servers_map(self):
        mcp_client.CONFIG_PATH.write_text(
            json.dumps({"mcpServers": {"test": {"command": "python3", "args": ["server.py"]}}})
        )
        self.assertEqual(
            mcp_client._load_config(),
            {"test": {"command": "python3", "args": ["server.py"]}},
        )

    def test_missing_mcp_servers_key_returns_empty(self):
        mcp_client.CONFIG_PATH.write_text(json.dumps({"somethingElse": {}}))
        self.assertEqual(mcp_client._load_config(), {})


class IsMcpToolTests(unittest.TestCase):
    def setUp(self):
        self._orig_clients = dict(mcp_client._clients)
        mcp_client._clients.clear()
        mcp_client._clients["myserver"] = object()  # any placeholder - only presence matters

    def tearDown(self):
        mcp_client._clients.clear()
        mcp_client._clients.update(self._orig_clients)

    def test_recognizes_a_qualified_tool_from_a_connected_server(self):
        self.assertTrue(mcp_client.is_mcp_tool("myserver__some_tool"))

    def test_rejects_builtin_tool_names(self):
        self.assertFalse(mcp_client.is_mcp_tool("bash"))
        self.assertFalse(mcp_client.is_mcp_tool("str_replace_based_edit_tool"))

    def test_rejects_a_name_from_an_unconnected_server(self):
        self.assertFalse(mcp_client.is_mcp_tool("otherserver__some_tool"))

    def test_name_with_no_separator_is_not_an_mcp_tool(self):
        self.assertFalse(mcp_client.is_mcp_tool("myserver"))  # no "__tool" part at all


def _fake_tool(name, description, input_schema):
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.input_schema = input_schema
    return tool


def _fake_client(tools):
    """A MagicMock standing in for mcp.Client - async context manager +
    async list_tools()/call_tool(), all backed by AsyncMock so _run()'s
    asyncio.run_coroutine_threadsafe(...) has real coroutines to await."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    list_result = MagicMock()
    list_result.tools = tools
    client.list_tools = AsyncMock(return_value=list_result)
    return client


class ConnectAllTests(unittest.TestCase):
    def setUp(self):
        self._orig_clients = dict(mcp_client._clients)
        mcp_client._clients.clear()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config_path = mcp_client.CONFIG_PATH
        mcp_client.CONFIG_PATH = Path(self._tmpdir.name) / "mcp_servers.json"

    def tearDown(self):
        mcp_client._clients.clear()
        mcp_client._clients.update(self._orig_clients)
        mcp_client.CONFIG_PATH = self._orig_config_path
        self._tmpdir.cleanup()

    def test_no_config_file_returns_no_tools(self):
        self.assertEqual(mcp_client.connect_all(), [])
        self.assertEqual(mcp_client._clients, {})

    def test_merges_and_namespaces_tools_from_one_server(self):
        mcp_client.CONFIG_PATH.write_text(
            json.dumps({"mcpServers": {"calc": {"command": "python3", "args": []}}})
        )
        fake = _fake_client([_fake_tool("add", "Add two numbers.", {"type": "object"})])
        with patch.object(mcp_client.mcp, "Client", return_value=fake):
            defs = mcp_client.connect_all()
        self.assertEqual(
            defs,
            [{"name": "calc__add", "description": "Add two numbers.", "input_schema": {"type": "object"}}],
        )
        self.assertIn("calc", mcp_client._clients)

    def test_one_broken_server_does_not_stop_the_others(self):
        mcp_client.CONFIG_PATH.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "broken": {"command": "does-not-exist", "args": []},
                        "good": {"command": "python3", "args": []},
                    }
                }
            )
        )
        good_client = _fake_client([_fake_tool("ping", "", {"type": "object"})])

        def fake_ctor(params):
            if params.command == "does-not-exist":
                broken = MagicMock()
                broken.__aenter__ = AsyncMock(side_effect=FileNotFoundError("no such command"))
                return broken
            return good_client

        with patch.object(mcp_client.mcp, "Client", side_effect=fake_ctor):
            defs = mcp_client.connect_all()
        self.assertEqual([d["name"] for d in defs], ["good__ping"])
        self.assertNotIn("broken", mcp_client._clients)
        self.assertIn("good", mcp_client._clients)


class CallToolTests(unittest.TestCase):
    def setUp(self):
        self._orig_clients = dict(mcp_client._clients)
        mcp_client._clients.clear()

    def tearDown(self):
        mcp_client._clients.clear()
        mcp_client._clients.update(self._orig_clients)

    def _register(self, server_name, call_tool_result):
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=call_tool_result)
        mcp_client._clients[server_name] = client
        return client

    def _text_result(self, text, is_error=False):
        block = MagicMock()
        block.type = "text"
        block.text = text
        result = MagicMock()
        result.content = [block]
        result.is_error = is_error
        return result

    def test_unknown_server_is_an_error_without_prompting(self):
        with patch("builtins.input") as mock_input:
            result = mcp_client.call_tool("noserver__foo", {})
        self.assertEqual(result, 'Error: no connected MCP server named "noserver"')
        mock_input.assert_not_called()

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="n")
    def test_declined_call_never_reaches_the_server(self, mock_input):
        import os as _os

        _os.environ.pop("AUTO_APPROVE_MCP", None)
        client = self._register("srv", self._text_result("should not see this"))
        result = mcp_client.call_tool("srv__tool", {"x": 1})
        self.assertEqual(result, "MCP call declined by the user - not run.")
        client.call_tool.assert_not_called()

    @patch.dict("os.environ", {}, clear=False)
    @patch("builtins.input", return_value="y")
    def test_approved_call_returns_text_content(self, mock_input):
        import os as _os

        _os.environ.pop("AUTO_APPROVE_MCP", None)
        self._register("srv", self._text_result("42"))
        result = mcp_client.call_tool("srv__tool", {"x": 1})
        self.assertEqual(result, "42")

    @patch.dict("os.environ", {"AUTO_APPROVE_MCP": "true"})
    def test_auto_approve_skips_the_prompt(self):
        self._register("srv", self._text_result("ok"))
        with patch("builtins.input") as mock_input:
            result = mcp_client.call_tool("srv__tool", {})
        self.assertEqual(result, "ok")
        mock_input.assert_not_called()

    @patch.dict("os.environ", {"AUTO_APPROVE_MCP": "true"})
    def test_server_side_error_is_prefixed(self):
        self._register("srv", self._text_result("bad input", is_error=True))
        result = mcp_client.call_tool("srv__tool", {})
        self.assertEqual(result, "Error: bad input")

    @patch.dict("os.environ", {"AUTO_APPROVE_MCP": "true"})
    def test_non_text_content_becomes_a_placeholder_not_silence(self):
        block = MagicMock()
        block.type = "image"
        result_obj = MagicMock()
        result_obj.content = [block]
        result_obj.is_error = False
        self._register("srv", result_obj)
        result = mcp_client.call_tool("srv__tool", {})
        self.assertIn("non-text MCP content", result)
        self.assertIn("image", result)

    @patch.dict("os.environ", {"AUTO_APPROVE_MCP": "true"})
    def test_exception_during_call_becomes_an_error_string_not_a_crash(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("connection dropped"))
        mcp_client._clients["srv"] = client
        result = mcp_client.call_tool("srv__tool", {})
        self.assertTrue(result.startswith("Error: MCP call to srv__tool failed"))


if __name__ == "__main__":
    unittest.main()
