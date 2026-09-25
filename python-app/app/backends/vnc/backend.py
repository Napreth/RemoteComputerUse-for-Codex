"""First-class VNC backend delegating library details to an adapter."""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..base import ComputerBackend
from ..config import VncConfig
from .adapter import VNCAdapter
from .keys import held_keysym, text_keysym
from .remote_control import VncRemoteControlAdapter

logger = logging.getLogger(__name__)


class VNCBackend(ComputerBackend):
    kind = "vnc"

    def __init__(
        self,
        adapter_factory: Callable[[], VNCAdapter] = VncRemoteControlAdapter,
    ) -> None:
        self.width = 0
        self.height = 0
        self._adapter_factory = adapter_factory
        self._adapter: VNCAdapter | None = None

    @property
    def connected(self) -> bool:
        return self._adapter is not None and self._adapter.connected

    def _require_adapter(self) -> VNCAdapter:
        if not self.connected:
            raise RuntimeError("VNC backend is not connected")
        assert self._adapter is not None
        return self._adapter

    async def connect(self, config: VncConfig) -> None:
        adapter = self._adapter_factory()
        try:
            await adapter.connect(config)
        except BaseException:
            try:
                await adapter.disconnect()
            except BaseException:
                logger.warning("VNC adapter cleanup after connection failure also failed", exc_info=True)
            raise
        self._adapter = adapter
        self.width = adapter.width
        self.height = adapter.height

    async def disconnect(self) -> None:
        adapter, self._adapter = self._adapter, None
        self.width = 0
        self.height = 0
        if adapter is not None:
            await adapter.disconnect()

    async def screenshot_png(self) -> bytes:
        return await self._require_adapter().screenshot_png()

    async def move(self, x: int, y: int) -> None:
        self.validate_point(x, y)
        await self._require_adapter().move(x, y)

    async def mouse_button(self, x: int, y: int, button: str, pressed: bool) -> None:
        self.validate_point(x, y)
        await self._require_adapter().mouse_button(x, y, button, pressed)

    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        self.validate_point(x, y)
        await self._require_adapter().scroll(x, y, delta_x, delta_y)

    async def type_text(self, text: str) -> None:
        await self._require_adapter().type_text(text)

    def validate_text(self, text: str) -> None:
        if len(text) > 10_000:
            raise ValueError("text is limited to 10,000 characters per call")
        for character in text:
            text_keysym(character)

    async def key_chord(self, keys: list[str]) -> None:
        await self._require_adapter().key_chord(keys)

    def validate_key_chord(self, keys: list[str]) -> None:
        if not keys or len(keys) > 8:
            raise ValueError("keypress requires between 1 and 8 keys")
        for key in keys:
            held_keysym(key)

    async def key_event(self, key: str, pressed: bool) -> None:
        await self._require_adapter().key_event(key, pressed)

    def key_identity(self, key: str) -> int:
        return held_keysym(key)

    async def release_inputs(self) -> None:
        await self._require_adapter().release_inputs()
