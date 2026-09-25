"""First-class RDP backend delegating library details to an adapter."""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..base import ComputerBackend
from ..config import RdpConfig
from .aardwolf import AardwolfAdapter
from .adapter import RDPAdapter
from .keys import key_scancode

logger = logging.getLogger(__name__)


class RDPBackend(ComputerBackend):
    kind = "rdp"

    def __init__(self, adapter_factory: Callable[[], RDPAdapter] = AardwolfAdapter) -> None:
        self.width = 0
        self.height = 0
        self._adapter_factory = adapter_factory
        self._adapter: RDPAdapter | None = None

    @property
    def connected(self) -> bool:
        return self._adapter is not None and self._adapter.connected

    async def connect(self, config: RdpConfig) -> None:
        adapter = self._adapter_factory()
        try:
            await adapter.connect(config)
        except BaseException:
            try:
                await adapter.disconnect()
            except BaseException:  # Preserve the original connection failure.
                logger.warning("RDP adapter cleanup after connection failure also failed", exc_info=True)
            raise
        self._adapter = adapter
        self.width = adapter.width
        self.height = adapter.height

    def _require_adapter(self) -> RDPAdapter:
        if not self.connected:
            raise RuntimeError("RDP backend is not connected")
        assert self._adapter is not None
        return self._adapter

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
        self.validate_scroll(delta_x, delta_y)
        await self._require_adapter().scroll(x, y, delta_x, delta_y)

    def validate_scroll(self, delta_x: int, delta_y: int) -> None:
        if delta_x:
            raise ValueError("the RDP adapter currently supports vertical scrolling only")

    async def type_text(self, text: str) -> None:
        await self._require_adapter().type_text(text)

    def validate_text(self, text: str) -> None:
        if len(text) > 10_000:
            raise ValueError("text is limited to 10,000 characters per call")
        text.encode("utf-16-le")

    async def key_chord(self, keys: list[str]) -> None:
        await self._require_adapter().key_chord(keys)

    def validate_key_chord(self, keys: list[str]) -> None:
        if not keys or len(keys) > 8:
            raise ValueError("keypress requires between 1 and 8 keys")
        for key in keys:
            key_scancode(key)

    async def key_event(self, key: str, pressed: bool) -> None:
        await self._require_adapter().key_event(key, pressed)

    def key_identity(self, key: str) -> tuple[int, bool] | None:
        resolved = key_scancode(key)
        return resolved[0] if resolved is not None else None

    async def release_inputs(self) -> None:
        await self._require_adapter().release_inputs()
