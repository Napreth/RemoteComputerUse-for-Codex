from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_manifest_command_starts_stdio_server() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    definition = manifest["mcpServers"]["remote_computer"]
    assert definition["command"] == "uv"
    assert definition["args"] == ["run", "--frozen", "--project", "./python-app", "remote-computer-use"]

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=root / "python-app",
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        tools_by_name = {tool.name: tool for tool in tools.tools}
        assert set(tools_by_name) == {
            "computer_status",
            "computer_list",
            "computer_connect",
            "computer_screenshot",
            "computer_exec_py",
            "computer_move",
            "computer_click",
            "computer_drag",
            "computer_scroll",
            "computer_type",
            "computer_keypress",
            "computer_run_actions",
            "computer_disconnect",
        }
        batch_schema = tools_by_name["computer_run_actions"].input_schema
        action_schema = batch_schema["properties"]["actions"]
        assert action_schema["minItems"] == 1
        assert action_schema["maxItems"] == 50
        item_schema = action_schema["items"]
        if "$ref" in item_schema:
            item_schema = batch_schema["$defs"][item_schema["$ref"].rsplit("/", 1)[-1]]
        assert item_schema["discriminator"]["propertyName"] == "type"
        assert len(item_schema["oneOf"]) == 7
        assert "name" in tools_by_name["computer_connect"].input_schema["properties"]
        for tool_name in tools_by_name.keys() - {"computer_connect", "computer_list"}:
            assert "session" in tools_by_name[tool_name].input_schema["properties"]

        invalid_batch = await client.call_tool(
            "computer_run_actions",
            {"actions": [{"type": "click", "x": 1, "y": 2, "foo": "not allowed"}]},
        )
        assert invalid_batch.is_error is True
        result = await client.call_tool("computer_status", {})
        assert result.is_error is False
        assert result.structured_content == {
            "connected": False,
            "backend": None,
            "host": None,
            "port": None,
            "width": None,
            "height": None,
        }
        listed = await client.call_tool("computer_list", {})
        assert listed.is_error is False
        assert listed.structured_content == {"sessions": []}
