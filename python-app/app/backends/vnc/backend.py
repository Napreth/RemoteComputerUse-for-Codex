"""Dedicated VNC/RFB backend using a pinned MIT RFB client implementation."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections.abc import Callable
from typing import Any

from PIL import Image
from vnc_remote_control.adapters.rfb import RfbClient

from ..base import ComputerBackend
from ..config import VncConfig
from .keys import keysym, text_keysym

_BUTTON_MASKS = {"left": 1, "middle": 2, "right": 4}
_VNC_CONNECT_GRACE = 1.0
logger = logging.getLogger(__name__)


class VNCBackend(ComputerBackend):
    kind = "vnc"

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self._client: RfbClient | None = None
        self._buttons = 0
        self._transport_failed = False

    @property
    def connected(self) -> bool:
        client = self._client
        if client is None or self._transport_failed:
            return False
        transport: Any = getattr(client, "_sock", None)
        if transport is None:
            return False
        try:
            return transport.fileno() >= 0
        except (OSError, ValueError):
            return False

    def _require_client(self) -> RfbClient:
        if not self.connected:
            raise RuntimeError("VNC backend is not connected")
        assert self._client is not None
        return self._client

    @staticmethod
    def _close_client(client: RfbClient) -> None:
        try:
            client.close()
        except Exception:  # Socket close remains best effort.
            logger.warning("VNC socket close failed", exc_info=True)

    @classmethod
    def _close_after_worker(cls, client: RfbClient, worker: asyncio.Future[Any]) -> None:
        try:
            worker.exception()
        except asyncio.CancelledError:
            logger.debug("Detached VNC worker was cancelled")
        except Exception:  # Consume a detached worker outcome.
            logger.debug("Detached VNC worker failed", exc_info=True)
        cls._close_client(client)

    @classmethod
    def _close_and_consume_worker(cls, client: RfbClient, worker: asyncio.Future[Any]) -> None:
        if worker.done():
            cls._close_after_worker(client, worker)
        else:
            worker.add_done_callback(lambda task: cls._close_after_worker(client, task))

    async def _run_io(self, client: RfbClient, operation: Callable[..., Any], *args: Any) -> Any:
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(worker)
        except BaseException:
            self._transport_failed = True
            self._close_client(client)
            self._close_and_consume_worker(client, worker)
            raise

    async def connect(self, config: VncConfig) -> None:
        if not config.shared:
            raise ValueError("the current VNC transport supports shared sessions only")
        client = RfbClient(
            config.host,
            config.port,
            timeout=config.connect_timeout,
            password=config.password or None,
        )
        worker = asyncio.create_task(asyncio.to_thread(client.connect))
        try:
            await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=config.connect_timeout + _VNC_CONNECT_GRACE,
            )
        except BaseException:
            self._close_client(client)
            self._close_and_consume_worker(client, worker)
            raise
        self._client = client
        self.width = client.width
        self.height = client.height
        self._buttons = 0
        self._transport_failed = False

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self.width = 0
        self.height = 0
        self._buttons = 0
        self._transport_failed = False
        if client is not None:
            self._close_client(client)

    def _screenshot_png(self) -> bytes:
        client = self._require_client()
        image = Image.frombytes("RGB", (client.width, client.height), bytes(client.capture()))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    async def screenshot_png(self) -> bytes:
        client = self._require_client()
        png = await self._run_io(client, self._screenshot_png)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("VNC backend returned an invalid PNG framebuffer")
        return png

    async def move(self, x: int, y: int) -> None:
        self.validate_point(x, y)
        client = self._require_client()
        # vnc-remote-control 1.0.4 exposes only private raw event helpers;
        # the exact dependency pin is the compatibility boundary for this MVP.
        await self._run_io(client, client._pointer, self._buttons, x, y)

    async def mouse_button(self, x: int, y: int, button: str, pressed: bool) -> None:
        self.validate_point(x, y)
        if button not in _BUTTON_MASKS:
            raise ValueError("button must be left, right, or middle")
        mask = _BUTTON_MASKS[button]
        self._buttons = self._buttons | mask if pressed else self._buttons & ~mask
        client = self._require_client()
        await self._run_io(client, client._pointer, self._buttons, x, y)
        # Some Windows VNC servers coalesce back-to-back PointerEvents. Keep a
        # short hold/release interval so clicks and drag endpoints are observed.
        await asyncio.sleep(0.03)

    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        self.validate_point(x, y)
        client = self._require_client()
        events: list[int] = []
        if delta_y:
            events.extend([16 if delta_y > 0 else 8] * max(1, min(20, abs(delta_y) // 120 or 1)))
        if delta_x:
            events.extend([64 if delta_x > 0 else 32] * max(1, min(20, abs(delta_x) // 120 or 1)))
        for wheel_mask in events:
            await self._run_io(client, client._pointer, self._buttons | wheel_mask, x, y)
            await self._run_io(client, client._pointer, self._buttons, x, y)

    async def type_text(self, text: str) -> None:
        if len(text) > 10_000:
            raise ValueError("text is limited to 10,000 characters per call")
        client = self._require_client()

        def write() -> None:
            for character in text:
                client.tap(0xFF0D if character == "\n" else text_keysym(character))

        await self._run_io(client, write)

    async def key_chord(self, keys: list[str]) -> None:
        if not keys or len(keys) > 8:
            raise ValueError("keypress requires between 1 and 8 keys")
        symbols = [keysym(key) for key in keys]
        client = self._require_client()

        def press() -> None:
            for symbol in symbols:
                client._key(symbol, down=True)
                time.sleep(0.01)
            for symbol in reversed(symbols):
                client._key(symbol, down=False)
                time.sleep(0.01)

        await self._run_io(client, press)

    async def release_inputs(self) -> None:
        client = self._require_client()

        def release() -> None:
            self._buttons = 0
            client._pointer(0, 0, 0)
            for name in ("ctrl", "shift", "alt", "win"):
                client._key(keysym(name), down=False)

        await self._run_io(client, release)
