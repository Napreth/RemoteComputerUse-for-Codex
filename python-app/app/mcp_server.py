"""MCP surface exposing named persistent remote computers."""

from __future__ import annotations

import asyncio
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
from .backends.config import Port, RdpHeight, RdpWidth
from .plugin_session import PluginSessions
from .profiles import list_profiles, load_profile

mcp = MCPServer(
    "Remote Computer Use",
    version=__version__,
    instructions=(
        "Operate isolated computers through named persistent Python runtimes. "
        "Prefer computer_exec_py so several actions, screenshots, checks, and loops can run in one call. "
        "Use computer_list_profiles to discover local profile names, then computer_connect(profile=..., name=...). "
        "An empty profile list allows computer_connect(name=...) with existing environment settings. "
        "A session argument is optional while exactly one computer is connected and required when there are several. "
        "RDP and VNC are independent backends selected at connection time; atomic tools use the selected session."
    ),
)
sessions = PluginSessions()


def _tool_error(error: Exception) -> ToolError:
    return ToolError(str(error))


@mcp.tool()
async def computer_status(session: str | None = None) -> dict[str, Any]:
    """Return connection state, selected backend, target, and native framebuffer dimensions."""
    try:
        return await sessions.status(session)
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_list() -> dict[str, list[dict[str, Any]]]:
    """List every named remote session owned by this server process."""
    return {"sessions": await sessions.list_sessions()}


@mcp.tool()
async def computer_list_profiles() -> dict[str, list[dict[str, str]]]:
    """List profile names/types without secrets; a missing config file returns an empty list."""
    try:
        return {"profiles": await asyncio.to_thread(list_profiles)}
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_connect(
    name: str | None = None,
    backend: str | None = None,
    host: str | None = None,
    port: Port | None = None,
    username: str | None = None,
    domain: str | None = None,
    width: RdpWidth | None = None,
    height: RdpHeight | None = None,
    profile: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
) -> dict[str, Any]:
    """Connect using a saved profile or explicit RDP/VNC settings; name identifies the runtime session."""
    try:
        if profile is not None:
            if any(value is not None for value in (backend, host, port, username, domain, width, height)):
                raise ValueError("profile cannot be combined with connection overrides; only name may be supplied.")
            config = await asyncio.to_thread(load_profile, profile)
        else:
            config = load_backend_config(
                backend, host=host, port=port, username=username, domain=domain, width=width, height=height,
            )
        return await sessions.connect(config, name)
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_screenshot(session: str | None = None) -> Image:
    """Capture the native remote framebuffer as PNG without touching the host screen."""
    try:
        return Image(data=await sessions.screenshot(session), format="png")
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_exec_py(
    code: Annotated[str, Field(min_length=1, max_length=65_536)],
    session: str | None = None,
) -> list[TextContent | Image]:
    """Execute Python in the persistent remote session; globals survive across calls."""
    try:
        result = await sessions.execute(code, session)
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
async def computer_move(x: int, y: int, session: str | None = None) -> str:
    """Move the remote pointer to native framebuffer coordinates."""
    try:
        await sessions.run_actions([MoveAction(type="move", x=x, y=y)], session)
        return f"Moved the remote pointer to ({x}, {y})."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_click(
    x: int,
    y: int,
    button: MouseButton = "left",
    count: ClickCount = 1,
    session: str | None = None,
) -> str:
    """Click the remote pointer; button is left, right, or middle and count is 1 or 2."""
    try:
        await sessions.run_actions(
            [ClickAction(type="click", x=x, y=y, button=button, count=count)],
            session,
        )
        return f"Clicked {button} at ({x}, {y}) {count} time(s)."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_drag(
    path: Annotated[list[Point], Field(min_length=2, max_length=100)],
    button: MouseButton = "left",
    duration_ms: Annotated[int, Field(ge=0, le=10_000)] = 300,
    session: str | None = None,
) -> str:
    """Drag through 2-100 native-coordinate points while holding one mouse button."""
    try:
        await sessions.run_actions(
            [DragAction(type="drag", path=path, button=button, duration_ms=duration_ms)],
            session,
        )
        return f"Dragged {button} through {len(path)} point(s)."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_scroll(
    x: int,
    y: int,
    delta_y: int,
    delta_x: int = 0,
    session: str | None = None,
) -> str:
    """Scroll at remote coordinates; positive delta values scroll down or right."""
    try:
        await sessions.run_actions(
            [ScrollAction(type="scroll", x=x, y=y, delta_x=delta_x, delta_y=delta_y)],
            session,
        )
        return f"Scrolled at ({x}, {y}) by ({delta_x}, {delta_y})."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_type(
    text: Annotated[str, Field(max_length=10_000)],
    session: str | None = None,
) -> str:
    """Type literal Unicode text into the focused remote control."""
    try:
        await sessions.run_actions([TypeAction(type="type", text=text)], session)
        return f"Typed {len(text)} character(s) into the remote computer."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_keypress(
    keys: Annotated[list[str], Field(min_length=1, max_length=8)],
    session: str | None = None,
) -> str:
    """Press a named key or chord such as ['ctrl', 'l'], ['win', 'r'], or ['enter']."""
    try:
        await sessions.run_actions([KeypressAction(type="keypress", keys=keys)], session)
        return f"Pressed remote key chord: {'+'.join(keys)}."
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_run_actions(
    actions: Annotated[list[ComputerAction], Field(min_length=1, max_length=50)],
    session: str | None = None,
) -> Image:
    """Run up to 50 bounded input/wait actions and return the resulting screenshot."""
    try:
        return Image(data=await sessions.run_actions(actions, session), format="png")
    except Exception as error:
        raise _tool_error(error) from error


@mcp.tool()
async def computer_disconnect(session: str | None = None) -> str:
    """Disconnect the active remote computer backend."""
    try:
        name = await sessions.disconnect(session)
        return f"Remote session '{name}' disconnected."
    except Exception as error:
        raise _tool_error(error) from error


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
