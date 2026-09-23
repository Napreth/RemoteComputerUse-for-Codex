"""MCP surface exposing one Computer API over the selected first-class backend."""

from __future__ import annotations

import base64
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent
from pydantic import Field

from . import __version__
from .actions import (
    ClickAction,
    ClickCount,
    ComputerAction,
    DragAction,
    KeypressAction,
    MouseButton,
    MoveAction,
    Point,
    ScrollAction,
    TypeAction,
)
from .backends import load_backend_config
from .plugin_session import PluginSession

mcp = MCPServer(
    "Remote Computer Use",
    version=__version__,
    instructions=(
        "Operate the configured isolated computer through one persistent Python runtime. "
        "Prefer computer_exec_py so several actions, screenshots, checks, and loops can run in one call. "
        "RDP and VNC are independent backends selected at connection time; atomic tools use the same session."
    ),
)
session = PluginSession()


def _tool_error(error: Exception) -> ToolError:
    return ToolError(str(error))


@mcp.tool()
async def computer_status() -> dict[str, Any]:
    """Return connection state, selected backend, target, and native framebuffer dimensions."""
    return await session.status()


@mcp.tool()
async def computer_connect(
    backend: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    domain: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Connect to the RDP or VNC backend using credentials from the server environment."""
    try:
        config = load_backend_config(
            backend,
            host=host,
            port=port,
            username=username,
            domain=domain,
            width=width,
            height=height,
        )
        return await session.connect(config)
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_screenshot() -> Image:
    """Capture the native remote framebuffer as PNG without touching the host screen."""
    try:
        return Image(data=await session.screenshot(), format="png")
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_exec_py(code: Annotated[str, Field(min_length=1, max_length=65_536)]) -> list[TextContent | Image]:
    """Execute Python in the persistent remote session; globals survive across calls."""
    try:
        result = await session.execute(code)
        content: list[TextContent | Image] = []
        for item in result["output"]:
            if item["type"] == "input_text":
                content.append(TextContent(type="text", text=item["text"]))
            else:
                encoded = item["image_url"].removeprefix("data:image/png;base64,")
                content.append(Image(data=base64.b64decode(encoded, validate=True), format="png"))
        return content
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_move(x: int, y: int) -> str:
    """Move the remote pointer to native framebuffer coordinates."""
    try:
        await session.run_actions([MoveAction(type="move", x=x, y=y)])
        return f"Moved the remote pointer to ({x}, {y})."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_click(x: int, y: int, button: MouseButton = "left", count: ClickCount = 1) -> str:
    """Click the remote pointer; button is left, right, or middle and count is 1 or 2."""
    try:
        await session.run_actions([ClickAction(type="click", x=x, y=y, button=button, count=count)])
        return f"Clicked {button} at ({x}, {y}) {count} time(s)."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_drag(
    path: Annotated[list[Point], Field(min_length=2, max_length=100)],
    button: MouseButton = "left",
    duration_ms: Annotated[int, Field(ge=0, le=10_000)] = 300,
) -> str:
    """Drag through 2-100 native-coordinate points while holding one mouse button."""
    try:
        await session.run_actions(
            [DragAction(type="drag", path=path, button=button, duration_ms=duration_ms)]
        )
        return f"Dragged {button} through {len(path)} point(s)."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_scroll(x: int, y: int, delta_y: int, delta_x: int = 0) -> str:
    """Scroll at remote coordinates; positive delta values scroll down or right."""
    try:
        await session.run_actions(
            [ScrollAction(type="scroll", x=x, y=y, delta_x=delta_x, delta_y=delta_y)]
        )
        return f"Scrolled at ({x}, {y}) by ({delta_x}, {delta_y})."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_type(text: Annotated[str, Field(max_length=10_000)]) -> str:
    """Type literal Unicode text into the focused remote control."""
    try:
        await session.run_actions([TypeAction(type="type", text=text)])
        return f"Typed {len(text)} character(s) into the remote computer."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_keypress(keys: Annotated[list[str], Field(min_length=1, max_length=8)]) -> str:
    """Press a named key or chord such as ['ctrl', 'l'], ['win', 'r'], or ['enter']."""
    try:
        await session.run_actions([KeypressAction(type="keypress", keys=keys)])
        return f"Pressed remote key chord: {'+'.join(keys)}."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_run_actions(
    actions: Annotated[list[ComputerAction], Field(min_length=1, max_length=50)],
) -> Image:
    """Run up to 50 bounded input/wait actions and return the resulting screenshot."""
    try:
        return Image(data=await session.run_actions(actions), format="png")
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_disconnect() -> str:
    """Disconnect the active remote computer backend."""
    try:
        await session.disconnect()
        return "Remote computer disconnected."
    except Exception as error:
        raise _tool_error(error) from error


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
