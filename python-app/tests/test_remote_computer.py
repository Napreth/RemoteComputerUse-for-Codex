from __future__ import annotations

import asyncio
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from PIL import Image

from app.backends import RdpConfig, VncConfig
from app.backends.rdp.backend import RDPBackend
from app.backends.vnc.backend import VNCBackend
from app.backends.vnc.remote_control import VncRemoteControlAdapter
from app.computer_session import ComputerSession
from app.remote.computer import RemoteComputer
from app.remote.worker import handle_request


@pytest.mark.parametrize("kind", ["rdp", "vnc"])
@pytest.mark.parametrize(("host", "expected"), [
    ("192.0.2.10", "192.0.2.10"), ("RDP.Example", "rdp.example"),
    ("::1", "::1"), ("[::1]", "::1"), ("2001:0db8::1", "2001:db8::1"),
])
def test_target_is_a_parseable_remote_uri(kind, host, expected):
    adapter = SimpleNamespace(width=64, height=48, connected=True, connect=AsyncMock(), disconnect=AsyncMock())
    backend = RDPBackend if kind == "rdp" else VNCBackend
    config = RdpConfig(host=host, port=3389, username="fixture") if kind == "rdp" else VncConfig(host=host, port=5900)
    session = ComputerSession({kind: lambda: backend(lambda: adapter)})
    computer = RemoteComputer(config, session=session)
    try:
        parsed = urlsplit(computer.target)
        assert (parsed.scheme, parsed.hostname, parsed.port) == (kind, expected, config.port)
        if ":" in expected:
            assert computer.target == f"{kind}://[{expected}]:{config.port}"
    finally:
        computer.close()


@pytest.fixture
def remote():
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48)).save(buffer, format="PNG")
    adapter = SimpleNamespace(
        width=64, height=48, connected=True,
        connect=AsyncMock(), disconnect=AsyncMock(),
        screenshot_png=AsyncMock(return_value=buffer.getvalue()),
        move=AsyncMock(), mouse_button=AsyncMock(), scroll=AsyncMock(),
        type_text=AsyncMock(), key_chord=AsyncMock(), key_event=AsyncMock(), release_inputs=AsyncMock(),
    )
    session = ComputerSession({"rdp": lambda: RDPBackend(lambda: adapter)})
    computer = RemoteComputer(RdpConfig(host="fixture", port=3389, username="fixture"), session=session)
    computer.PAUSE = 0
    try:
        yield computer, adapter
    finally:
        computer.close()


@pytest.mark.parametrize("steps", [1, 99])
@pytest.mark.parametrize("duration", [0, 0.05, 0.12])
def test_explicit_drag_steps_control_trajectory_and_duration(remote, monkeypatch, steps, duration):
    computer, adapter = remote
    computer.moveTo(2, 4)
    adapter.move.reset_mock()
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.remote.input_compat.asyncio.sleep", sleep)
    tween = Mock(side_effect=lambda value: value)
    computer.drag_to((40, 30), duration=duration, steps=steps, tween=tween)
    assert adapter.move.await_count == steps
    assert adapter.move.await_args.args == (40, 30)
    assert [call.args[0] for call in tween.call_args_list] == [index / steps for index in range(1, steps + 1)]
    assert adapter.mouse_button.await_args_list[-2].args == (2, 4, "left", True)
    assert adapter.mouse_button.await_args_list[-1].args == (40, 30, "left", False)
    assert sum(sleeps) == pytest.approx(duration)


def test_drag_without_steps_keeps_duration_based_sampling(remote, monkeypatch):
    computer, adapter = remote

    async def sleep(delay):
        pass

    monkeypatch.setattr("app.remote.input_compat.asyncio.sleep", sleep)
    computer.dragTo(40, 30, duration=0.12)
    assert adapter.move.await_count == 6


@pytest.mark.parametrize("steps", [0, 100, 1.5])
def test_invalid_drag_steps_do_not_send_input(remote, steps):
    computer, adapter = remote
    with pytest.raises((ValueError, TypeError)):
        computer.drag_to(40, 30, steps=steps)
    adapter.move.assert_not_awaited()
    adapter.mouse_button.assert_not_awaited()


@pytest.fixture
def vnc_remote():
    client = SimpleNamespace(
        _sock=SimpleNamespace(fileno=lambda: 7),
        _key=Mock(), tap=Mock(),
    )
    adapter = VncRemoteControlAdapter()
    adapter._client = client
    adapter.width, adapter.height = 64, 48
    adapter.connect = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.release_inputs = AsyncMock()
    session = ComputerSession({"vnc": lambda: VNCBackend(lambda: adapter)})
    computer = RemoteComputer(VncConfig(host="fixture", port=5900), session=session)
    computer.PAUSE = 0
    try:
        yield computer, adapter, client
    finally:
        computer.close()


def test_invalid_vnc_text_does_not_hold_input_or_attempt_release(vnc_remote):
    computer, adapter, client = vnc_remote

    with pytest.raises(ValueError, match="unsupported VNC text control"):
        computer.write("\x01")

    assert computer.status()["inputHeld"] is False
    computer.close()
    client.tap.assert_not_called()
    adapter.release_inputs.assert_not_awaited()


@pytest.mark.parametrize("keys", [("\x01", "a"), ("a", "\x01")])
def test_invalid_hotkey_key_does_not_hold_input_or_attempt_release(vnc_remote, keys):
    computer, adapter, client = vnc_remote

    with pytest.raises(ValueError, match="held VNC keys"):
        computer.hotkey(*keys)

    assert computer.status()["inputHeld"] is False
    computer.close()
    client._key.assert_not_called()
    adapter.release_inputs.assert_not_awaited()


def test_invalid_vnc_press_does_not_hold_input_or_attempt_release(vnc_remote):
    computer, adapter, client = vnc_remote

    with pytest.raises(ValueError, match="held VNC keys"):
        computer.press("\x01")

    assert computer.status()["inputHeld"] is False
    computer.close()
    client._key.assert_not_called()
    adapter.release_inputs.assert_not_awaited()


def test_vnc_control_key_literal_matches_text_and_key_calls(vnc_remote):
    computer, adapter, client = vnc_remote

    computer.write("\t")
    computer.press("\t")
    computer.keyDown("\t")
    computer.keyUp("\t")
    computer.hotkey("\t")

    assert client.tap.call_args.args == (0xFF09,)
    assert [call.args for call in client._key.call_args_list] == [
        (0xFF09,), (0xFF09,), (0xFF09,), (0xFF09,), (0xFF09,), (0xFF09,),
    ]
    assert [call.kwargs for call in client._key.call_args_list] == [
        {"down": True}, {"down": False},
        {"down": True}, {"down": False},
        {"down": True}, {"down": False},
    ]
    assert computer.status()["inputHeld"] is False
    computer.close()
    adapter.release_inputs.assert_not_awaited()


@pytest.mark.parametrize("action,position", [
    ({"type": "move", "x": 20, "y": 30}, (20, 30)),
    ({"type": "click", "x": 21, "y": 31}, (21, 31)),
    ({"type": "drag", "path": [{"x": 10, "y": 10}, {"x": 22, "y": 32}], "duration_ms": 0}, (22, 32)),
    ({"type": "scroll", "x": 23, "y": 33, "delta_y": 120}, (23, 33)),
])
def test_atomic_actions_and_python_share_pointer_position(remote, action, position):
    computer, adapter = remote
    handle_request({"operation": "actions", "actions": [action]}, {"computer": computer})
    computer.click()
    assert adapter.mouse_button.await_args_list[-1].args[:2] == position
    computer.scroll(120)
    assert adapter.scroll.await_args.args[:2] == position
    computer.drag_to(40, 40, duration=0, steps=1)
    assert adapter.mouse_button.await_args_list[-2].args[:2] == position


def test_partial_batch_failure_retains_successful_pointer_move(remote):
    computer, adapter = remote
    result = handle_request({"operation": "actions", "actions": [
        {"type": "move", "x": 20, "y": 30},
        {"type": "move", "x": 64, "y": 30},
    ]}, {"computer": computer})
    assert result["error"]["recoverable"] is True
    computer.click()
    assert adapter.mouse_button.await_args_list[-1].args[:2] == (20, 30)


def test_on_screen_uses_current_coordinate_for_missing_axis(remote):
    computer, _adapter = remote
    computer.moveTo(10, 20)

    assert computer.onScreen(30) is True
    assert computer.onScreen(None, 30) is True
    assert computer.onScreen(None) is True
    assert computer.onScreen(64) is False
    assert computer.onScreen(None, 48) is False


def test_upstream_scroll_direction_and_notches_are_preserved(remote):
    computer, adapter = remote
    computer.scroll(45, 10, 20)
    assert [call.args for call in adapter.scroll.await_args_list] == [
        (10, 20, 0, -2400), (10, 20, 0, -2400), (10, 20, 0, -600),
    ]
    computer.scroll(-10)
    assert adapter.scroll.await_args.args == (10, 20, 0, 1200)


def test_unsupported_rdp_hscroll_does_not_move_pointer(remote):
    computer, adapter = remote
    computer.moveTo(10, 10)

    with pytest.raises(ValueError, match="vertical scrolling only"):
        computer.hscroll(1, 30, 40)

    assert computer.position() == (10, 10)
    assert [call.args for call in adapter.move.await_args_list] == [(10, 10)]
    adapter.scroll.assert_not_awaited()
    computer.click()
    assert adapter.mouse_button.await_args_list[-1].args[:2] == (10, 10)


def test_rdp_control_key_literals_work_across_facade_calls(remote):
    computer, adapter = remote

    computer.press("\t")
    computer.keyDown("\n")
    computer.keyUp("\n")
    computer.hotkey("ctrl", "\b")
    computer.typewrite(["\t"])

    assert [call.args[0] for call in adapter.key_chord.await_args_list] == [["\t"], ["\t"]]
    assert [call.args for call in adapter.key_event.await_args_list] == [
        ("\n", True), ("\n", False),
        ("ctrl", True), ("\b", True), ("\b", False), ("ctrl", False),
    ]
    assert computer.status()["inputHeld"] is False


def test_rdp_carriage_return_is_noop_across_facade_calls(remote):
    computer, adapter = remote

    computer.press("\r")
    computer.write("\r")
    computer.typewrite(["\r"])
    computer.keyDown("\r")
    assert computer.status()["inputHeld"] is False
    computer.keyUp("\r")
    computer.hotkey("\r")

    adapter.key_event.assert_not_awaited()
    assert computer.status()["inputHeld"] is False
    computer.close()
    adapter.release_inputs.assert_not_awaited()


def test_upstream_repeated_clicks_and_sequential_keys(remote):
    computer, adapter = remote
    computer.click(10, 20, clicks=3, interval=0)
    assert adapter.mouse_button.await_count == 6
    computer.press(["left", "right"], presses=2, interval=0)
    assert [call.args[0] for call in adapter.key_chord.await_args_list] == [
        ["left"], ["right"], ["left"], ["right"],
    ]


@pytest.mark.parametrize(("clicks", "interval"), [(1, 0.5), (3, 0.25)])
def test_click_interval_follows_every_button_release(remote, monkeypatch, clicks, interval):
    computer, adapter = remote
    events = []

    async def mouse_button(_x, _y, _button, pressed):
        events.append("down" if pressed else "up")

    async def sleep(delay):
        events.append(("sleep", delay))

    adapter.mouse_button.side_effect = mouse_button
    monkeypatch.setattr("app.remote.input_compat.asyncio.sleep", sleep)
    if clicks == 1:
        computer.rightClick(interval=interval)
    else:
        computer.click(clicks=clicks, interval=interval)

    assert events == [event for _ in range(clicks) for event in ("down", "up", ("sleep", interval))]


@pytest.mark.parametrize(("button", "expected"), [
    ("LEFT", "left"), ("MIDDLE", "middle"), ("RIGHT", "right"),
    ("PRIMARY", "left"), ("SECONDARY", "right"),
    (1, "left"), (2, "middle"), (3, "right"),
])
def test_pyautogui_button_aliases_reach_backend_canonically(remote, button, expected):
    from app.remote.worker import RemotePyAutoGUI

    computer, adapter = remote
    pyautogui = RemotePyAutoGUI(computer)
    assert (pyautogui.LEFT, pyautogui.MIDDLE, pyautogui.RIGHT,
            pyautogui.PRIMARY, pyautogui.SECONDARY) == (
        "left", "middle", "right", "primary", "secondary",
    )

    pyautogui.click(button=button)
    pyautogui.mouseDown(button=button)
    pyautogui.mouseUp(button=button)

    assert [call.args[2:] for call in adapter.mouse_button.await_args_list] == [
        (expected, True), (expected, False),
        (expected, True), (expected, False),
    ]
    assert computer.status()["inputHeld"] is False


def test_button_aliases_pair_across_down_up_and_reject_invalid_values(remote):
    computer, adapter = remote
    computer.mouseDown(button="PRIMARY")
    assert computer.status()["inputHeld"] is True
    computer.mouseUp(button=1)
    assert computer.status()["inputHeld"] is False

    adapter.move.reset_mock()
    adapter.mouse_button.reset_mock()
    for invalid in ("unknown", 4, True):
        with pytest.raises(ValueError, match="button must be"):
            computer.click(10, 20, button=invalid)
    adapter.move.assert_not_awaited()
    adapter.mouse_button.assert_not_awaited()


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


def test_hold_releases_in_input_order(remote):
    computer, adapter = remote
    with pytest.raises(ValueError), computer.hold(["ctrl", "shift"]):
        raise ValueError("body failed")
    assert [call.args for call in adapter.key_event.await_args_list] == [
        ("ctrl", True), ("shift", True), ("ctrl", False), ("shift", False),
    ]
    assert computer.status()["inputHeld"] is False


@pytest.mark.parametrize("operation", ["click", "partial_click", "scroll", "drag_to"])
def test_default_pointer_actions_are_atomic_across_python_threads(remote, monkeypatch, operation):
    computer, adapter = remote
    entered, queued = threading.Event(), threading.Event()
    release = asyncio.Event()

    async def move(x, y):
        if (x, y) == (10, 10):
            entered.set()
            await release.wait()

    adapter.move.side_effect = move

    def observed_call(coroutine):
        # Signal after submission, without changing the event-loop scheduling.
        future = asyncio.run_coroutine_threadsafe(coroutine, computer._loop)
        queued.set()
        return future.result(timeout=5)

    monkeypatch.setattr(computer, "_call", observed_call)
    actions = {
        "click": lambda: computer.click(),
        "partial_click": lambda: computer.click(y=15),
        "scroll": lambda: computer.scroll(120),
        "drag_to": lambda: computer.drag_to(40, 40, duration=0, steps=1),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(computer.move_to, 10, 10)
        try:
            assert entered.wait(2)
            queued.clear()
            action = pool.submit(actions[operation])
            assert queued.wait(2)
            queued.clear()
            last = pool.submit(computer.move_to, 30, 30)
            assert queued.wait(2)
        finally:
            computer._loop.call_soon_threadsafe(release.set)
        for future in (first, action, last):
            future.result(timeout=5)
    # The queued action must finish before the later move, without returning
    # to an earlier position read by a separate position() request.
    assert computer.position() == (30, 30)
    if operation == "scroll":
        assert adapter.scroll.await_args.args[:2] == (10, 10)
    else:
        expected = (10, 15) if operation == "partial_click" else (10, 10)
        assert adapter.mouse_button.await_args_list[0].args[:2] == expected


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


@pytest.mark.parametrize("operation", ["press", "write", "write_interval", "atomic_click", "drag_path"])
def test_failed_input_remains_pending_until_confirmed_release(remote, operation):
    computer, adapter = remote
    async def fail(*args, **kwargs):
        raise RuntimeError("release not acknowledged")
    actions = {
        "press": lambda: computer.press("a"),
        "write": lambda: computer.write("a"),
        "write_interval": lambda: computer.write("a", interval=0.01),
        "atomic_click": lambda: computer.run_actions([{"type": "click", "x": 1, "y": 2}]),
        "drag_path": lambda: computer.drag_path([(1, 2), (3, 4)], duration=0),
    }
    adapter.key_chord.side_effect = fail
    adapter.type_text.side_effect = fail
    adapter.mouse_button.side_effect = fail
    with pytest.raises(RuntimeError, match="release not acknowledged"):
        actions[operation]()
    assert computer.status()["inputHeld"] is True
    computer.screenshot()
    assert computer.status()["inputHeld"] is True
    computer.close()
    adapter.release_inputs.assert_awaited_once()


def test_failed_input_state_clears_only_after_successful_explicit_release(remote):
    computer, adapter = remote
    adapter.key_chord.side_effect = RuntimeError("key release failed")
    with pytest.raises(RuntimeError):
        computer.press("a")
    adapter.key_chord.side_effect = None
    computer.press("b")
    assert computer.status()["inputHeld"] is True
    adapter.release_inputs.side_effect = RuntimeError("release failed")
    with pytest.raises(RuntimeError):
        computer._call(computer._session.release_inputs())
    assert computer.status()["inputHeld"] is True
    adapter.release_inputs.side_effect = None
    computer._call(computer._session.release_inputs())
    assert computer.status()["inputHeld"] is False


def test_character_key_events_preserve_case(remote):
    computer, adapter = remote
    computer.keyDown("A")
    computer.keyUp("A")
    assert [call.args for call in adapter.key_event.await_args_list] == [("A", True), ("A", False)]


@pytest.mark.parametrize(("pressed", "released"), [("A", "a"), ("escape", "esc"), ("control", "ctrl")])
def test_equivalent_rdp_key_spellings_clear_held_identity(remote, pressed, released):
    computer, adapter = remote
    computer.keyDown(pressed)
    assert computer.status()["inputHeld"] is True

    computer.keyUp(released)

    assert computer.status()["inputHeld"] is False
    computer.close()
    adapter.release_inputs.assert_not_awaited()


@pytest.mark.parametrize(("method", "button"), [
    ("leftClick", "left"), ("rightClick", "right"), ("middleClick", "middle"),
])
def test_single_button_clicks_keep_upstream_interval_position(remote, monkeypatch, method, button):
    computer, _adapter = remote
    click = Mock()
    monkeypatch.setattr(computer, "click", click)

    getattr(computer, method)(10, 20, 0.5)

    click.assert_called_once()
    assert click.call_args.args == (10, 20)
    assert click.call_args.kwargs["button"] == button
    assert click.call_args.kwargs["interval"] == 0.5
    assert click.call_args.kwargs["duration"] == 0


def test_position_overrides_are_truncated_to_integer_without_moving(remote):
    computer, adapter = remote
    computer.moveTo(7, 8)
    adapter.move.reset_mock()

    assert computer.position(10.9, -20.9) == (10, -20)
    assert computer.position(y=4.9) == (7, 4)
    assert computer.position() == (7, 8)
    adapter.move.assert_not_awaited()


def test_mouse_down_up_accept_upstream_duration_and_tween_but_move_instantly(remote):
    computer, adapter = remote

    def unused_tween(_value):
        raise AssertionError("PyAutoGUI 0.9.54 does not tween mouseDown/mouseUp")

    computer.mouseDown((10, 20), duration=0.5, tween=unused_tween)
    computer.mouseUp((10, 20), duration=0.5, tween=unused_tween)

    assert adapter.move.await_count == 2
    assert [call.args for call in adapter.mouse_button.await_args_list] == [
        (10, 20, "left", True), (10, 20, "left", False),
    ]


def test_typewrite_accepts_key_name_list(remote):
    computer, adapter = remote
    computer.typewrite(["a", "ENTER", "left"], interval=0)
    assert [call.args for call in adapter.key_chord.await_args_list] == [(["a"],), (["enter"],), (["left"],)]
    adapter.type_text.assert_not_awaited()


@pytest.mark.parametrize("method", ["click", "doubleClick", "tripleClick", "rightClick", "leftClick", "middleClick"])
def test_click_family_supports_tuple_duration_and_tween(remote, method):
    computer, adapter = remote
    fractions = []
    def tween(value):
        fractions.append(value)
        return value
    getattr(computer, method)((20, 30), duration=0.12, tween=tween)
    assert len(fractions) > 1
    assert computer.position() == (20, 30)
    assert adapter.mouse_button.await_args.args[:2] == (20, 30)


def test_tuple_coordinates_and_hotkey_reverse_release(remote):
    computer, adapter = remote
    computer.moveTo((10, 20))
    computer.moveRel((2, 3))
    computer.dragRel((2, 3))
    assert computer.position() == (14, 26)
    computer.dragTo((20, 30))
    computer.scroll(1, (5, 6))
    assert computer.position() == (5, 6)
    assert computer.onScreen((63, 47))
    computer.hotkey("ctrl", "shift")
    assert [call.args for call in adapter.key_event.await_args_list][-2:] == [("shift", False), ("ctrl", False)]
    with pytest.raises(ValueError):
        computer.click((1, 2), 3)
