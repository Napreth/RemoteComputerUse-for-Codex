from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.backends import RdpConfig
from app.computer_session import ComputerSession
from app.remote.computer import RemoteComputer


@pytest.fixture
def remote():
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48)).save(buffer, format="PNG")

    def validate(x, y):
        if not 0 <= x < 64 or not 0 <= y < 48:
            raise ValueError("outside framebuffer")

    adapter = SimpleNamespace(
        width=64, height=48, connected=True, kind="rdp",
        connect=AsyncMock(), disconnect=AsyncMock(), validate_point=validate,
        screenshot_png=AsyncMock(return_value=buffer.getvalue()),
        move=AsyncMock(), mouse_button=AsyncMock(), scroll=AsyncMock(),
        type_text=AsyncMock(), key_chord=AsyncMock(), key_event=AsyncMock(), release_inputs=AsyncMock(),
    )
    session = ComputerSession({"rdp": lambda: adapter})
    computer = RemoteComputer(RdpConfig(host="fixture", port=3389, username="fixture"), session=session)
    computer.PAUSE = 0
    try:
        yield computer, adapter
    finally:
        computer.close()


def test_upstream_scroll_direction_and_notches_are_preserved(remote):
    computer, adapter = remote
    computer.scroll(45, 10, 20)
    assert [call.args for call in adapter.scroll.await_args_list] == [
        (10, 20, 0, -2400), (10, 20, 0, -2400), (10, 20, 0, -600),
    ]
    computer.scroll(-10)
    assert adapter.scroll.await_args.args == (10, 20, 0, 1200)



def test_upstream_repeated_clicks_and_sequential_keys(remote):
    computer, adapter = remote
    computer.click(10, 20, clicks=3, interval=0)
    assert adapter.mouse_button.await_count == 6
    computer.press(["left", "right"], presses=2, interval=0)
    assert [call.args[0] for call in adapter.key_chord.await_args_list] == [
        ["left"], ["right"], ["left"], ["right"],
    ]



def test_upstream_screenshot_file_and_relative_movement(remote, tmp_path):
    computer, _adapter = remote
    computer.moveTo(10, 20, duration=0.01)
    computer.moveRel(3, -2)
    assert computer.position() == (13, 18)
    computer.dragRel(2, 1, duration=0)
    assert computer.position() == (15, 19)
    path = tmp_path / "shot.png"
    frame = computer.screenshot(str(path), region=(1, 2, 10, 11))
    assert frame.size == (10, 11)
    with Image.open(path) as saved:
        assert saved.size == frame.size



def test_invalid_tween_is_rejected_before_button_down(remote):
    computer, adapter = remote
    with pytest.raises(ValueError):
        computer.dragTo(60, 40, duration=0.2, tween=lambda _: 10)
    adapter.mouse_button.assert_not_awaited()



def test_held_keys_and_buttons_persist_and_are_released_on_close(remote):
    computer, adapter = remote
    computer.keyDown("shift")
    computer.mouseDown(10, 20)
    assert computer.status()["inputHeld"] is True
    computer.keyUp("shift")
    assert computer.status()["inputHeld"] is True
    computer.close()
    adapter.release_inputs.assert_awaited_once()



def test_hold_and_hotkey_release_in_reverse_order(remote):
    computer, adapter = remote
    with pytest.raises(ValueError), computer.hold(["ctrl", "shift"]):
        raise ValueError("body failed")
    assert [call.args for call in adapter.key_event.await_args_list] == [
        ("ctrl", True), ("shift", True), ("shift", False), ("ctrl", False),
    ]
    assert computer.status()["inputHeld"] is False



def test_imported_pyautogui_uses_remote_connection_and_shared_pause(remote, monkeypatch):
    import importlib
    import sys

    from app.remote.worker import RemotePyAutoGUI

    computer, adapter = remote
    proxy = RemotePyAutoGUI(computer)
    monkeypatch.setitem(sys.modules, "pyautogui", proxy)
    imported = importlib.import_module("pyautogui")
    imported.PAUSE = 0
    imported.moveTo(11, 12)
    assert computer.position() == (11, 12)
    assert imported.position() == computer.position()
    adapter.move.assert_awaited_with(11, 12)
    assert computer.PAUSE == 0
    with pytest.raises(AttributeError):
        imported.locateOnScreen("missing.png")
