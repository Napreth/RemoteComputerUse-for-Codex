"""Unified Computer API over independent RDP and VNC backend instances."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .actions import (
    ClickAction,
    ComputerAction,
    DragAction,
    KeypressAction,
    MoveAction,
    Point,
    ScrollAction,
    TypeAction,
    WaitAction,
)
from .backends import BackendConfig, ComputerBackend, RDPBackend, VNCBackend

BackendFactory = Callable[[], ComputerBackend]
logger = logging.getLogger(__name__)


class ComputerSession:
    def __init__(self, factories: dict[str, BackendFactory] | None = None) -> None:
        self._factories = factories or {"rdp": RDPBackend, "vnc": VNCBackend}
        self._backend: ComputerBackend | None = None
        self._config: BackendConfig | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._backend is not None and self._backend.connected

    def status(self) -> dict[str, Any]:
        backend = self._backend
        config = self._config
        return {
            "connected": backend is not None and backend.connected,
            "backend": backend.kind if backend else None,
            "host": config.host if config else None,
            "port": config.port if config else None,
            "width": backend.width if backend else None,
            "height": backend.height if backend else None,
        }

    async def connect(self, config: BackendConfig) -> dict[str, Any]:
        async with self._lock:
            old_backend, self._backend = self._backend, None
            self._config = None
            if old_backend is not None:
                await old_backend.disconnect()
            factory = self._factories.get(config.kind)
            if factory is None:
                raise ValueError(f"no backend is registered for {config.kind!r}")
            backend = factory()
            try:
                await backend.connect(config)
            except BaseException:
                try:
                    await backend.disconnect()
                except BaseException:  # Preserve the original connection failure.
                    logger.warning("Backend cleanup after connection failure also failed", exc_info=True)
                raise
            self._backend = backend
            self._config = config
            return self.status()

    async def disconnect(self) -> None:
        async with self._lock:
            backend, self._backend = self._backend, None
            self._config = None
            if backend is not None:
                await backend.disconnect()

    def _require_backend(self) -> ComputerBackend:
        if self._backend is None or not self._backend.connected:
            raise RuntimeError("remote computer is not connected; call computer_connect first")
        return self._backend

    async def screenshot(self) -> bytes:
        async with self._lock:
            return await self._require_backend().screenshot_png()

    async def move(self, x: int, y: int) -> None:
        async with self._lock:
            await self._move(x, y)

    async def _move(self, x: int, y: int) -> None:
        await self._require_backend().move(x, y)

    async def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        async with self._lock:
            await self._click(x, y, button, count)

    async def _click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        if count not in {1, 2}:
            raise ValueError("click count must be 1 or 2")
        backend = self._require_backend()
        await backend.move(x, y)
        for index in range(count):
            try:
                # A cancelled button-down may already have reached the server.
                # Enter the protected region before sending it so a release is
                # attempted even when the down call itself is interrupted.
                await backend.mouse_button(x, y, button, True)
            except BaseException:
                try:
                    await backend.mouse_button(x, y, button, False)
                except BaseException:  # Preserve the first release failure.
                    logger.warning("Mouse-button release after button-down failure also failed", exc_info=True)
                raise
            else:
                await backend.mouse_button(x, y, button, False)
            if index + 1 < count:
                await asyncio.sleep(0.08)

    async def drag(self, path: list[Point], button: str = "left", duration_ms: int = 300) -> None:
        async with self._lock:
            await self._drag(path, button, duration_ms)

    async def _drag(self, path: list[Point], button: str = "left", duration_ms: int = 300) -> None:
        if not 2 <= len(path) <= 100:
            raise ValueError("drag path must contain between 2 and 100 points")
        if not 0 <= duration_ms <= 10_000:
            raise ValueError("drag duration_ms must be between 0 and 10000")
        points = [(point.x, point.y) for point in path]
        delay = duration_ms / 1000 / max(1, len(points) - 1)
        backend = self._require_backend()
        start_x, start_y = points[0]
        await backend.move(start_x, start_y)
        end_x, end_y = start_x, start_y
        try:
            # Treat the button as potentially pressed before awaiting the down
            # event; cancellation can arrive after the server observes it.
            await backend.mouse_button(start_x, start_y, button, True)
            for end_x, end_y in points[1:]:
                await backend.move(end_x, end_y)
                if delay:
                    await asyncio.sleep(delay)
        except BaseException:
            try:
                await backend.mouse_button(end_x, end_y, button, False)
            except BaseException:  # Preserve the drag failure.
                logger.warning("Mouse-button release after drag failure also failed", exc_info=True)
            raise
        else:
            await backend.mouse_button(end_x, end_y, button, False)

    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        async with self._lock:
            await self._scroll(x, y, delta_x, delta_y)

    async def _scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        await self._require_backend().scroll(x, y, delta_x, delta_y)

    async def type_text(self, text: str) -> None:
        async with self._lock:
            await self._type_text(text)

    async def _type_text(self, text: str) -> None:
        await self._require_backend().type_text(text)

    async def keypress(self, keys: list[str]) -> None:
        async with self._lock:
            await self._keypress(keys)

    async def _keypress(self, keys: list[str]) -> None:
        await self._require_backend().key_chord(keys)

    async def release_inputs(self) -> None:
        async with self._lock:
            await self._require_backend().release_inputs()

    async def run_actions(self, actions: list[ComputerAction]) -> bytes:
        if not actions or len(actions) > 50:
            raise ValueError("actions must contain between 1 and 50 items")
        async with self._lock:
            waited = 0.0
            for action in actions:
                if isinstance(action, MoveAction):
                    await self._move(action.x, action.y)
                elif isinstance(action, ClickAction):
                    await self._click(action.x, action.y, action.button, action.count)
                elif isinstance(action, DragAction):
                    await self._drag(action.path, action.button, action.duration_ms)
                elif isinstance(action, ScrollAction):
                    await self._scroll(action.x, action.y, action.delta_x, action.delta_y)
                elif isinstance(action, TypeAction):
                    await self._type_text(action.text)
                elif isinstance(action, KeypressAction):
                    await self._keypress(action.keys)
                elif isinstance(action, WaitAction):
                    waited += action.seconds
                    if waited > 15:
                        raise ValueError("total batch wait time is limited to 15 seconds")
                    await asyncio.sleep(action.seconds)
            return await self._require_backend().screenshot_png()
