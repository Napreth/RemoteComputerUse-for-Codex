from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.guest import open_url


async def test_windows_rdp_opens_url_through_run_dialog(monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTER_GUEST_OS", "windows")
    ready = AsyncMock(return_value=b"png")
    monkeypatch.setattr("app.guest.windows.wait_for_visual_ready", ready)
    execution = SimpleNamespace(
        backend="rdp",
        width=1440,
        height=900,
        run_actions=AsyncMock(return_value=b"png"),
    )

    await open_url(execution, "http://192.0.2.1:4567/index.html")

    ready.assert_awaited_once_with(execution)
    actions = execution.run_actions.await_args.args[0]
    assert actions[0] == {"type": "keypress", "keys": ["win", "r"]}
    assert actions[2] == {
        "type": "type",
        "text": (
            'msedge.exe --no-first-run --no-default-browser-check '
            '"http://192.0.2.1:4567/index.html"'
        ),
    }


async def test_windows_vnc_requires_explicit_shortcut_mapping(monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTER_GUEST_OS", "windows")
    monkeypatch.delenv("REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION", raising=False)
    execution = SimpleNamespace(backend="vnc", run_actions=AsyncMock())
    ready = AsyncMock(return_value=b"png")
    monkeypatch.setattr("app.guest.windows.wait_for_visual_ready", ready)
    with pytest.raises(RuntimeError, match="VNC Windows navigation is not configured"):
        await open_url(execution, "http://192.0.2.1/")
    execution.run_actions.assert_not_awaited()
    ready.assert_not_awaited()
    monkeypatch.setenv("REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION", "win-r")
    await open_url(execution, "http://192.0.2.1/")
    actions = execution.run_actions.await_args.args[0]
    assert actions[0] == {"type": "keypress", "keys": ["win", "r"]}
    assert all(action["type"] != "click" for action in actions)


async def test_guest_os_workflow_must_be_explicit(monkeypatch):
    monkeypatch.delenv("REMOTE_COMPUTER_GUEST_OS", raising=False)
    execution = SimpleNamespace(run_actions=AsyncMock())

    with pytest.raises(RuntimeError, match="REMOTE_COMPUTER_GUEST_OS=windows"):
        await open_url(execution, "http://192.0.2.1/")

    execution.run_actions.assert_not_awaited()
