from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.actions import Point, parse_computer_action
from app.backends.base import ComputerBackend
from app.backends.config import RdpConfig
from app.computer_session import ComputerSession


class FakeBackend(ComputerBackend):
    kind = "rdp"

    def __init__(self) -> None:
        self.width = 100
        self.height = 80
        self.events: list[tuple[Any, ...]] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, config: Any) -> None:
        self._connected = True
        self.events.append(("connect", config.host))

    async def disconnect(self) -> None:
        self._connected = False
        self.events.append(("disconnect",))

    async def screenshot_png(self) -> bytes:
        self.events.append(("screenshot",))
        return b"\x89PNG\r\n\x1a\nmock"

    async def move(self, x: int, y: int) -> None:
        self.validate_point(x, y)
        self.events.append(("move", x, y))

    async def mouse_button(self, x: int, y: int, button: str, pressed: bool) -> None:
        self.events.append(("button", button, pressed, x, y))

    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        self.events.append(("scroll", x, y, delta_x, delta_y))

    async def type_text(self, text: str) -> None:
        self.events.append(("type", text))

    async def key_chord(self, keys: list[str]) -> None:
        self.events.append(("keys", *keys))

    async def release_inputs(self) -> None:
        self.events.append(("release_inputs",))


@pytest.mark.asyncio
async def test_unified_session_runs_actions_against_registered_backend() -> None:
    backend = FakeBackend()
    session = ComputerSession({"rdp": lambda: backend})
    status = await session.connect(RdpConfig(host="target", port=3389, username="user"))

    image = await session.run_actions(
        [
            parse_computer_action({"type": "click", "x": 10, "y": 20, "button": "left"}),
            parse_computer_action({"type": "type", "text": "hello"}),
            parse_computer_action({"type": "keypress", "keys": ["ctrl", "a"]}),
            parse_computer_action({"type": "scroll", "x": 10, "y": 20, "delta_y": 120}),
        ]
    )
    await session.disconnect()

    assert status == {
        "connected": True,
        "backend": "rdp",
        "host": "target",
        "port": 3389,
        "width": 100,
        "height": 80,
    }
    assert image.startswith(b"\x89PNG")
    assert ("type", "hello") in backend.events
    assert backend.events[-1] == ("disconnect",)


@pytest.mark.asyncio
async def test_drag_always_releases_button() -> None:
    backend = FakeBackend()
    session = ComputerSession({"rdp": lambda: backend})
    await session.connect(RdpConfig(host="target", port=3389, username="user"))

    await session.drag([Point(x=1, y=2), Point(x=3, y=4)], duration_ms=0)

    assert backend.events[-1] == ("button", "left", False, 3, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["click", "drag"])
async def test_cancellation_during_button_down_still_releases_mouse(operation: str) -> None:
    backend = FakeBackend()
    session = ComputerSession({"rdp": lambda: backend})
    await session.connect(RdpConfig(host="target", port=3389, username="user"))
    down_delivered = asyncio.Event()

    async def interrupted_button(x: int, y: int, button: str, pressed: bool) -> None:
        backend.events.append(("button", button, pressed, x, y))
        if pressed:
            down_delivered.set()
            await asyncio.Event().wait()

    backend.mouse_button = interrupted_button  # type: ignore[method-assign]
    if operation == "click":
        task = asyncio.create_task(session.click(10, 20))
    else:
        task = asyncio.create_task(
            session.drag([Point(x=10, y=20), Point(x=30, y=40)], duration_ms=0)
        )
    await down_delivered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.events[-1] == ("button", "left", False, 10, 20)


@pytest.mark.asyncio
async def test_status_uses_backend_transport_health() -> None:
    backend = FakeBackend()
    session = ComputerSession({"rdp": lambda: backend})
    await session.connect(RdpConfig(host="target", port=3389, username="user"))

    backend._connected = False

    assert session.connected is False
    assert session.status()["connected"] is False
    with pytest.raises(RuntimeError, match="not connected"):
        await session.screenshot()


@pytest.mark.asyncio
async def test_reconnect_detaches_old_backend_before_cleanup_io() -> None:
    backend = FakeBackend()
    session = ComputerSession({"rdp": lambda: backend})
    await session.connect(RdpConfig(host="old", port=3389, username="user"))

    async def fail_disconnect() -> None:
        raise RuntimeError("old cleanup failed")

    backend.disconnect = fail_disconnect  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="old cleanup failed"):
        await session.connect(RdpConfig(host="new", port=3389, username="user"))

    assert session.status() == {
        "connected": False,
        "backend": None,
        "host": None,
        "port": None,
        "width": None,
        "height": None,
    }
    status = await session.connect(RdpConfig(host="replacement", port=3389, username="user"))
    assert status["connected"] is True
    assert status["host"] == "replacement"


@pytest.mark.asyncio
async def test_connect_cleanup_error_does_not_mask_original_failure() -> None:
    class FailingBackend(FakeBackend):
        async def connect(self, config: Any) -> None:
            raise RuntimeError("original connect failure")

        async def disconnect(self) -> None:
            raise RuntimeError("secondary cleanup failure")

    session = ComputerSession({"rdp": FailingBackend})

    with pytest.raises(RuntimeError, match="original connect failure"):
        await session.connect(RdpConfig(host="target", port=3389, username="user"))

    assert session.connected is False
