from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from app import mcp_server


@pytest.mark.parametrize("field", ["port", "width", "height"])
@pytest.mark.parametrize("value", [True, False, 0, 1.5, 4000.0])
async def test_connect_rejects_invalid_numbers_before_launch(monkeypatch, field, value):
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "env-rdp.example")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "env-user")
    monkeypatch.setenv(f"REMOTE_COMPUTER_RDP_{field.upper()}", "4000")
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    with pytest.raises(ToolError):
        await mcp_server.mcp.call_tool("computer_connect", {field: value, "backend": "rdp"})
    connect.assert_not_awaited()


async def test_connect_preserves_valid_overrides_and_empty_domain_through_mcp(monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "env-rdp.example")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "env-user")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_DOMAIN", "ENV-DOMAIN")
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    await mcp_server.mcp.call_tool("computer_connect", {
        "backend": "rdp", "domain": "", "name": "work", "host": "explicit.example", "username": "explicit-user",
        "port": 3392, "width": 1800, "height": 1200,
    })
    config, name = connect.await_args.args
    assert (config.host, config.username, config.domain, config.port, config.width, config.height, name) == (
        "explicit.example", "explicit-user", "", 3392, 1800, 1200, "work",
    )


@pytest.mark.parametrize(("tool", "arguments"), [
    ("computer_click", {"x": True, "y": 2}),
    ("computer_move", {"x": 1.5, "y": 2}),
    ("computer_click", {"x": "1", "y": 2}),
    ("computer_scroll", {"x": 1, "y": 2, "delta_y": 120.0}),
])
async def test_mcp_rejects_wrong_json_number_types_before_sending_input(monkeypatch, tool, arguments):
    actions = AsyncMock()
    monkeypatch.setattr(mcp_server.sessions, "run_actions", actions)
    with pytest.raises(ToolError):
        await mcp_server.mcp.call_tool(tool, arguments)
    actions.assert_not_awaited()


async def test_strict_mcp_preserves_valid_action_models_and_screenshot_output(monkeypatch):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    actions = AsyncMock(return_value=buffer.getvalue())
    monkeypatch.setattr(mcp_server.sessions, "run_actions", actions)
    result = await mcp_server.mcp.call_tool("computer_run_actions", {"actions": [
        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}], "duration_ms": 12},
    ]})
    assert result.content[0].type == "image"
    assert actions.await_args.args[0][0].path[1].x == 3


@asynccontextmanager
async def _startup_client(parameters: StdioServerParameters, budget: float = 30):
    async with AsyncExitStack() as stack:
        # The deadline includes spawning uv and dependency synchronization, not
        # just the JSON-RPC initialize request. Tool calls retain a 300s timeout.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        async with asyncio.timeout_at(deadline):
            read, write = await stack.enter_async_context(stdio_client(parameters))
            client = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=300))
            await client.initialize()
        if loop.time() > deadline:
            raise TimeoutError(f"Bundled MCP startup exceeded {budget:g} seconds")
        yield client


@pytest.mark.parametrize("stage", ["spawn", "initialize", "combined"])
async def test_startup_budget_includes_spawn_and_initialize(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    @asynccontextmanager
    async def delayed_stdio(parameters):
        if stage == "spawn":
            await asyncio.Event().wait()
        if stage == "combined":
            await asyncio.sleep(0.06)
        yield object(), object()

    class DelayedClient:
        def __init__(self, read, write, *, read_timeout_seconds):
            assert read_timeout_seconds == 300

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            if stage == "initialize":
                await asyncio.Event().wait()
            await asyncio.sleep(0.06)

    monkeypatch.setattr(sys.modules[__name__], "stdio_client", delayed_stdio)
    monkeypatch.setattr(sys.modules[__name__], "ClientSession", DelayedClient)
    async with asyncio.timeout(2):
        with pytest.raises(TimeoutError):
            async with _startup_client(StdioServerParameters(command="unused"), budget=0.1):
                pytest.fail("Startup exceeded its shared spawn/initialize budget")


@pytest.mark.parametrize(("fill", "direct"), [(False, False), (True, False), (False, True), (True, True)])
async def test_real_mcp_process_exposes_only_user_enabled_agent_tools(tmp_path: Path, fill: bool, direct: bool):
    config = tmp_path / "remote-computer-use" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "version": 1, "enableDeclareAsFillByAgent": fill, "enableProfileFromAgent": direct,
        "profiles": [{"name": "template", "type": "rdp", "username": "alice", "fillByAgent": ["host", "password"]}],
    }), encoding="utf-8")
    before = config.read_bytes()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("REMOTE_COMPUTER_")}
    environment.update({"LOCALAPPDATA": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"],
        cwd=Path(__file__).parents[1], env=environment,
    )
    async with _startup_client(parameters) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert len(tools) == 14 + int(fill) + int(direct)
        assert ("computer_connect_filled_profile" in tools) is fill
        assert ("computer_connect_from_agent" in tools) is direct
        assert "password" not in tools["computer_connect"].input_schema["properties"]
        listed = await client.call_tool("computer_list_profiles", {})
        expected = {"name": "template", "type": "rdp"}
        if fill:
            expected["fillByAgent"] = ["host", "password"]
        assert listed.structured_content == {"profiles": [expected]}
        # Invalid authority must fail before worker launch; never connect to a VM.
        for tool, arguments in (
            ("computer_connect_filled_profile", {
                "profile": "template", "fields": {"host": "localhost@evil.example", "password": "secret-needle"},
            }),
            ("computer_connect_from_agent", {
                "backend": "rdp", "host": "localhost@evil.example", "username": "alice", "password": "secret-needle",
            }),
        ):
            rejected = await client.call_tool(tool, arguments)
            assert rejected.is_error is True
            assert "secret-needle" not in str(rejected.content)
        for tool, arguments in (
            ("computer_connect_filled_profile", {"fields": {"password": "secret-needle"}}),
            ("computer_connect_from_agent", {"password": "secret-needle"}),
            ("computer_connect_from_agent", {"backend": "vnc", "host": "localhost", "password": {"secret-needle": True}}),
        ):
            rejected = await client.call_tool(tool, arguments)
            assert rejected.is_error is True
            assert "secret-needle" not in str(rejected.content)
        assert (await client.call_tool("computer_list", {})).structured_content == {"sessions": []}
    assert config.read_bytes() == before


def test_agent_plugin_manifest_uses_root_components() -> None:
    root = Path(__file__).parents[2]
    plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (root / "python-app/tests/fixtures/agent_plugins_1_0_0_plugin.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(plugin)
    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin["name"] == "remote-computer-use"
    # Release versions change independently of the pinned Agent Plugins schema.
    assert plugin["version"].strip()
    assert "interface" in plugin["extensions"]["com.openai"]
    assert (root / "skills" / "remote-computer" / "SKILL.md").is_file()
    assert "mcpServers" not in plugin
    assert (root / "mcp.json").is_file()
    assert not (root / ".codex-plugin" / "plugin.json").exists()
    assert not (root / ".mcp.json").exists()
    assert not (root / "python-app" / "app" / "plugin_bootstrap.py").exists()
    assert not (root / "python-app" / "tests" / "test_plugin_bootstrap.py").exists()
    assert "REMOTE_COMPUTER_" not in json.dumps(plugin)
    assert "UV_" not in json.dumps(plugin)


def test_bundled_mcp_manifest_conforms_to_pinned_schema() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (root / "python-app/tests/fixtures/agent_plugins_1_0_0_mcp.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(manifest)
    assert manifest["mcpServers"] == {
        "remote_computer": {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "--frozen", "--no-dev", "--project", "${PLUGIN_ROOT}/python-app", "remote-computer-use"],
        }
    }


@pytest.mark.parametrize(
    "installation",
    [
        "existing-environment",
        pytest.param(
            "fresh-defaults",
            marks=pytest.mark.skipif(
                os.environ.get("REMOTE_COMPUTER_FRESH_INSTALL_TEST") != "1",
                reason="Fresh dependency installation runs in CI with network access",
            ),
        ),
        pytest.param(
            "fresh-host-uv",
            marks=pytest.mark.skipif(
                os.environ.get("REMOTE_COMPUTER_FRESH_INSTALL_TEST") != "1",
                reason="Fresh dependency installation runs in CI with network access",
            ),
        ),
        pytest.param(
            "cold-cache",
            marks=pytest.mark.skipif(
                os.environ.get("REMOTE_COMPUTER_COLD_INSTALL_TEST") != "1",
                reason="Cold-cache release compatibility check requires network access",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_bundled_package_starts_and_lists_tools(tmp_path: Path, installation: str) -> None:
    source = Path(__file__).parents[2]
    root = tmp_path / f"random plugin install {uuid4().hex}"
    fresh_package = installation != "existing-environment"
    package = tmp_path / "plugin.zip"
    await asyncio.to_thread(
        subprocess.check_call, [sys.executable, "-I", "-S", str(source / "scripts/package_plugin.py"), "--output", str(package)],
    )
    with zipfile.ZipFile(package) as archive:
        archive.extractall(root)
    assert not (root / "python-app" / ".venv").exists()
    manifest = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    definition = manifest["mcpServers"]["remote_computer"]

    # Observe the actual MCP process from Python startup through orderly exit.
    # This test-only hook neither changes the shipped runtime nor exposes a tool.
    probe = tmp_path / "environment-probe"
    probe.mkdir()
    report = probe / "runtime-environment.json"
    (probe / "sitecustomize.py").write_text(
        "import atexit, json, os, sys\n"
        "from pathlib import Path\n"
        "if Path(sys.argv[0]).name in {'remote-computer-use', 'remote-computer-use.exe'}:\n"
        "    def tracked_environment():\n"
        "        return {k: v for k, v in os.environ.items() if k.startswith(('UV_', 'REMOTE_COMPUTER_'))}\n"
        "    before = tracked_environment()\n"
        "    def report_environment():\n"
        f"        Path({str(report)!r}).write_text(json.dumps({{'before': before, 'after': tracked_environment(), 'python_version': list(sys.version_info[:2])}}), encoding='utf-8')\n"
        "    atexit.register(report_environment)\n",
        encoding="utf-8",
    )

    process_env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("REMOTE_COMPUTER_", "UV_"))
        and name not in {"PLUGIN_ROOT", "PLUGIN_DATA", "VIRTUAL_ENV"}
    }
    # Simulate Host path expansion and environment injection, without uv defaults
    # or connection settings supplied by the plugin.
    # Keep the real Host PATH: prepending pytest's interpreter would mask a ZIP
    # missing its Python version file when another Python appears first.
    process_env["PLUGIN_ROOT"] = str(root)
    process_env["PYTHONPATH"] = str(probe)
    # Package startup is tested without user configuration. Do not read the
    # developer's real profiles or let their feature switches change this test.
    process_env["LOCALAPPDATA"] = str(tmp_path / "test-user-config")
    process_env["XDG_CONFIG_HOME"] = str(tmp_path / "test-user-config")
    host_uv_values: dict[str, str] = {}
    expected_environment_path = root / "python-app" / ".venv"
    if installation == "cold-cache":
        host_uv_values = {"UV_CACHE_DIR": str(tmp_path / "empty uv cache"), "UV_PYTHON": sys.executable}
        assert not Path(host_uv_values["UV_CACHE_DIR"]).exists()
    elif installation == "fresh-host-uv":
        expected_environment_path = tmp_path / "host python environment"
        # A Host may select a shared cache. Reuse the installed controller's
        # cache here; the target virtual environment is still created from zero.
        shared_cache = (
            await asyncio.to_thread(
                subprocess.check_output, [definition["command"], "cache", "dir"], env=process_env, text=True,
            )
        ).strip()
        host_uv_values = {
            "UV_PROJECT_ENVIRONMENT": str(expected_environment_path),
            "UV_CACHE_DIR": shared_cache,
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_INSTALL_DIR": str(tmp_path / "host python installs"),
        }
    if not fresh_package:
        # Reuse dependency packages without reinstalling the copied project into
        # pytest's own environment. The fresh variant performs the real sync.
        host_uv_values = {"UV_PROJECT_ENVIRONMENT": sys.prefix, "UV_NO_SYNC": "1", "UV_PYTHON": sys.executable}
        process_env["PYTHONPATH"] += os.pathsep + str(root / "python-app")
    process_env.update(host_uv_values)
    original_host_environment = process_env.copy()
    host_cwd = tmp_path / "unrelated host working directory"
    host_cwd.mkdir()

    parameters = StdioServerParameters(
        command=definition["command"],
        args=[argument.replace("${PLUGIN_ROOT}", str(root)) for argument in definition["args"]],
        cwd=host_cwd,
        env=process_env,
    )

    async with _startup_client(parameters) as client:
        tools = await client.list_tools()
        tools_by_name = {tool.name: tool for tool in tools.tools}
        assert set(tools_by_name) == {
            "computer_status",
            "computer_list",
            "computer_list_profiles",
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
        assert "profile" in tools_by_name["computer_connect"].input_schema["properties"]
        assert "password" not in tools_by_name["computer_connect"].input_schema["properties"]
        assert "passwdenv" not in tools_by_name["computer_connect"].input_schema["properties"]
        for tool_name in tools_by_name.keys() - {"computer_connect", "computer_list", "computer_list_profiles"}:
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
        missing_config = await client.call_tool("computer_connect", {})
        assert missing_config.is_error is True
        error_text = str(missing_config.content).lower()
        assert any(word in error_text for word in ("required", "missing", "configuration"))
        assert any(word in error_text for word in ("rdp", "vnc", "backend"))

    if fresh_package:
        assert (expected_environment_path / "pyvenv.cfg").is_file()
    if installation == "fresh-host-uv":
        assert not (root / "python-app" / ".venv").exists()
    assert process_env == original_host_environment
    observations = json.loads(report.read_text(encoding="utf-8"))
    assert observations["before"] == observations["after"]
    for name, value in host_uv_values.items():
        assert observations["after"][name] == value
    if installation == "fresh-defaults":
        assert observations["python_version"] == [3, 12]
        assert not any(name.startswith("UV_") for name in process_env)
        for name in ("UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_INSTALL_DIR"):
            assert name not in observations["after"]
