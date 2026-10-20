"""MCP surface exposing named persistent remote computers."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp_types import CallToolResult, Icon, InputRequiredResult, TextContent, ToolAnnotations
from pydantic import Field, SecretStr, ValidationError

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
from .backends.config import BackendConfig, BackendKind, Port, RdpHeight, RdpWidth
from .plugin_session import PluginSessions
from .profiles import (
    ProfilesError,
    agent_features,
    list_profiles,
    load_agent_profile,
    load_filled_profile,
    load_profile,
)


class _LiteralToolMetadata(FuncMetadata):
    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        # MCP arguments are already JSON values. In particular, do not turn
        # optional strings or SecretStr passwords such as "null" into None.
        return data


class RemoteComputerMCPServer(MCPServer):
    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        # MCP 2.2.0's public add_tool returns no metadata handle; keep this
        # version-specific manager access here instead of patching the SDK.
        tool = self._tool_manager.add_tool(
            fn, name=name, title=title, description=description, annotations=annotations,
            icons=icons, meta=meta, structured_output=structured_output,
        )
        metadata = tool.fn_metadata
        argument_model = metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_config["strict"] = True
        argument_model.model_config["hide_input_in_errors"] = True
        argument_model.model_rebuild(force=True)
        tool.fn_metadata = _LiteralToolMetadata(
            arg_model=argument_model, output_schema=metadata.output_schema,
            output_model=metadata.output_model, wrap_output=metadata.wrap_output,
        )
        tool.parameters = argument_model.model_json_schema(by_alias=True)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            # SDK argument errors include the raw request, which may contain a
            # password even when a different required argument is missing.
            if name in {"computer_connect_filled_profile", "computer_connect_from_agent"} and isinstance(
                error.__cause__, ValidationError,
            ):
                raise ToolError(f"Invalid arguments for {name}; check required fields and value types.") from None
            raise


mcp = RemoteComputerMCPServer(
    "Remote Computer Use",
    version=__version__,
    instructions=(
        "Operate isolated computers through named persistent Python runtimes. "
        "Prefer computer_exec_py so several actions, screenshots, checks, and loops can run in one call. "
        "Use computer_list_profiles to discover local profile names, then computer_connect(profile=..., name=...). "
        "If a profile lists fillByAgent, use computer_connect_filled_profile with exactly those fields when available. "
        "computer_connect_from_agent is available only when explicitly enabled by the user. "
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
async def computer_list_profiles() -> dict[str, list[dict[str, Any]]]:
    """List profile names/types and enabled fillByAgent field names, without saved values or secrets."""
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
    except Exception as error:
        raise _tool_error(error) from error
    return await _connect_config(config, name)


async def _connect_config(config: BackendConfig, name: str | None) -> dict[str, Any]:
    try:
        return await sessions.connect(config, name)
    except Exception as error:  # noqa: BLE001 - redact backend failures before returning them through MCP
        message = str(error)
        if config.password:
            for secret in (config.password, quote(config.password, safe="")):
                message = message.replace(secret, "[redacted]")
        raise ToolError(message) from None


async def computer_connect_filled_profile(
    profile: Annotated[str, Field(min_length=1, max_length=128)],
    fields: dict[str, Any],
    name: str | None = None,
) -> dict[str, Any]:
    """Connect by supplying exactly the selected profile's fillByAgent fields; never saves them to disk."""
    try:
        config = await asyncio.to_thread(load_filled_profile, profile, fields)
    except Exception as error:
        raise _tool_error(error) from error
    return await _connect_config(config, name)


async def computer_connect_from_agent(
    backend: BackendKind,
    host: str,
    name: str | None = None,
    port: Port | None = None,
    username: str | None = None,
    password: SecretStr | None = None,
    domain: str | None = None,
    width: RdpWidth | None = None,
    height: RdpHeight | None = None,
    insecure_tls: Annotated[bool | None, Field(strict=True)] = None,
    shared: Annotated[bool | None, Field(strict=True)] = None,
) -> dict[str, Any]:
    """Connect with Agent-provided RDP/VNC settings, including password, without saving a profile or reading env defaults."""
    values = {key: value for key, value in {
        "type": backend, "host": host, "port": port, "username": username, "password": password,
        "domain": domain, "width": width, "height": height, "insecure_tls": insecure_tls, "shared": shared,
    }.items() if value is not None}
    try:
        config = await asyncio.to_thread(load_agent_profile, values)
    except Exception as error:
        raise _tool_error(error) from error
    return await _connect_config(config, name)


def register_agent_tools(server: RemoteComputerMCPServer) -> None:
    # Invalid/missing user configuration must not prevent MCP initialization.
    # Connection calls re-read it and enforce the current switches each time.
    try:
        fill_enabled, direct_enabled = agent_features()
    except ProfilesError:
        return
    if fill_enabled:
        server.add_tool(computer_connect_filled_profile)
    if direct_enabled:
        server.add_tool(computer_connect_from_agent)


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
    register_agent_tools(mcp)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
