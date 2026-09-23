from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions import ClickAction
from app.backends import RdpConfig
from app.plugin_session import PluginSession


@pytest.mark.asyncio
async def test_code_and_atomic_actions_share_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = SimpleNamespace(
        connected=True,
        close=AsyncMock(),
        status=AsyncMock(
            return_value={
                "connected": True,
                "backend": "rdp",
                "host": "target",
                "port": 3389,
                "width": 1440,
                "height": 900,
            }
        ),
        execute=AsyncMock(return_value={"output": [{"type": "input_text", "text": "42"}]}),
        run_actions=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
        capture_screenshot=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
    )
    launch = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.plugin_session.launch_remote_python_runtime", launch)
    session = PluginSession()
    config = RdpConfig(host="target", port=3389, username="codex")

    status = await session.connect(config)
    result = await session.execute("counter = 41\nlog(counter + 1)")
    image = await session.run_actions([ClickAction(type="click", x=10, y=20)])
    await session.disconnect()

    assert status["connected"] is True
    assert result["output"][0]["text"] == "42"
    assert image.startswith(b"\x89PNG")
    launch.assert_awaited_once_with(config)
    runtime.execute.assert_awaited_once()
    runtime.run_actions.assert_awaited_once_with(
        [{"type": "click", "x": 10, "y": 20, "button": "left", "count": 1}]
    )
    runtime.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_is_disconnected_without_worker() -> None:
    assert await PluginSession().status() == {
        "connected": False,
        "backend": None,
        "host": None,
        "port": None,
        "width": None,
        "height": None,
    }
