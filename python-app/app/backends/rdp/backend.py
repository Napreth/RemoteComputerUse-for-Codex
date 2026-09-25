"""Headless RDP backend backed exclusively by aardwolf's RDP client."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
from typing import Any
from urllib.parse import quote

from ..base import ComputerBackend
from ..config import RdpConfig
from .keys import SCANCODES

_RDP_TERMINATE_TIMEOUT = 5.0
_RDP_HARD_CLOSE_TIMEOUT = 1.0
logger = logging.getLogger(__name__)


class _Utf16CodeUnit:
    """Adapter for aardwolf 0.2.13's string-shaped Unicode input API."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __getitem__(self, index: int) -> _Utf16CodeUnit:
        if index != 0:
            raise IndexError(index)
        return self

    def encode(self, encoding: str) -> bytes:
        if encoding.lower().replace("_", "-") != "utf-16-le":
            raise ValueError(f"unsupported encoding: {encoding}")
        return self.value.to_bytes(2, "little")


def _host_for_url(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{address}]" if address.version == 6 else host


def connection_url(config: RdpConfig) -> str:
    username = f"{config.domain}\\{config.username}" if config.domain else config.username
    authority = quote(username, safe="\\")
    if config.password:
        authority += ":" + quote(config.password, safe="")
    return f"rdp+ntlm-password://{authority}@{_host_for_url(config.host)}:{config.port}"


def _unwrap(result: Any, operation: str) -> Any:
    if isinstance(result, tuple) and len(result) == 2:
        value, error = result
        if error is not None:
            raise RuntimeError(f"{operation} failed: {error}") from error
        return value
    return result


class RDPBackend(ComputerBackend):
    kind = "rdp"

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self._connection: Any | None = None
        self._mouse_buttons: Any | None = None
        self._video_formats: Any | None = None

    @property
    def connected(self) -> bool:
        connection = self._connection
        if connection is None:
            return False
        try:
            return not connection.disconnected_evt.is_set()
        except (AttributeError, RuntimeError):
            return False

    async def connect(self, config: RdpConfig) -> None:
        from aardwolf.commons.factory import RDPConnectionFactory
        from aardwolf.commons.iosettings import RDPIOSettings
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON, VIDEO_FORMAT

        settings = RDPIOSettings()
        settings.video_width = config.width
        settings.video_height = config.height
        settings.video_out_format = VIDEO_FORMAT.PIL
        settings.clipboard_use_pyperclip = False

        factory = RDPConnectionFactory.from_url(connection_url(config), settings)
        factory.target.unsafe_ssl = config.insecure_tls
        connection = factory.get_connection(settings)
        connect_task = asyncio.create_task(connection.connect())
        try:
            connected, error = await asyncio.wait_for(asyncio.shield(connect_task), config.connect_timeout)
            if error is not None or not connected:
                raise RuntimeError(f"RDP connection failed: {error or 'connection was rejected'}")
            await self._wait_for_first_frame(connection, config.first_frame_timeout)
        except BaseException:
            if not connect_task.done():
                connect_task.cancel()
                connect_task.add_done_callback(self._consume_task_result)
            try:
                await self._terminate_connection(connection)
            except BaseException:  # Preserve the original connection failure.
                logger.warning("RDP cleanup after connection failure also failed", exc_info=True)
            raise

        self._connection = connection
        self._mouse_buttons = MOUSEBUTTON
        self._video_formats = VIDEO_FORMAT
        self.width = config.width
        self.height = config.height

    async def _wait_for_first_frame(self, connection: Any, timeout: float) -> None:
        async def wait() -> None:
            while not connection.desktop_buffer_has_data:
                if connection.disconnected_evt.is_set():
                    raise RuntimeError("RDP disconnected before the first framebuffer update")
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait(), timeout)

    def _require_connection(self) -> Any:
        if not self.connected:
            raise RuntimeError("RDP backend is not connected")
        assert self._connection is not None
        return self._connection

    def _require_mouse_buttons(self) -> Any:
        if self._mouse_buttons is None:
            raise RuntimeError("RDP backend is not connected")
        return self._mouse_buttons

    def _require_video_formats(self) -> Any:
        if self._video_formats is None:
            raise RuntimeError("RDP backend is not connected")
        return self._video_formats

    async def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        self._mouse_buttons = None
        self._video_formats = None
        self.width = 0
        self.height = 0
        if connection is not None:
            await self._terminate_connection(connection)

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            return
        except Exception:  # Consume a detached cleanup task outcome.
            logger.debug("Detached RDP cleanup task failed", exc_info=True)

    async def _hard_close(self, connection: Any) -> None:
        disconnected = getattr(connection, "disconnected_evt", None)
        if disconnected is not None:
            disconnected.set()
        transport = getattr(connection, "_RDPConnection__connection", None)
        if transport is None:
            return
        try:
            result = transport.close()
            if not inspect.isawaitable(result):
                return
            task = asyncio.ensure_future(result)
            done, _ = await asyncio.wait({task}, timeout=_RDP_HARD_CLOSE_TIMEOUT)
            if task not in done:
                task.cancel()
                task.add_done_callback(self._consume_task_result)
            else:
                task.result()
        except Exception:  # Hard close is best effort after graceful cleanup failed.
            logger.warning("RDP hard-close fallback failed", exc_info=True)
            return

    async def _terminate_connection(self, connection: Any) -> None:
        task = asyncio.create_task(connection.terminate())
        try:
            done, _ = await asyncio.wait({task}, timeout=_RDP_TERMINATE_TIMEOUT)
        except BaseException:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
            await self._hard_close(connection)
            raise
        if task in done:
            _unwrap(task.result(), "RDP disconnect")
            return
        task.cancel()
        task.add_done_callback(self._consume_task_result)
        await self._hard_close(connection)
        raise TimeoutError(f"RDP disconnect exceeded {_RDP_TERMINATE_TIMEOUT:g} seconds; transport was closed")

    async def screenshot_png(self) -> bytes:
        video_formats = self._require_video_formats()
        png = _unwrap(self._require_connection().get_desktop_buffer(video_formats.PNG), "RDP screenshot")
        if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("RDP backend returned an invalid PNG framebuffer")
        return png

    async def move(self, x: int, y: int) -> None:
        self.validate_point(x, y)
        mouse_buttons = self._require_mouse_buttons()
        _unwrap(
            await self._require_connection().send_mouse(mouse_buttons.MOUSEBUTTON_HOVER, x, y, False),
            "RDP pointer move",
        )

    async def mouse_button(self, x: int, y: int, button: str, pressed: bool) -> None:
        self.validate_point(x, y)
        mouse_buttons = self._require_mouse_buttons()
        choices = {
            "left": mouse_buttons.MOUSEBUTTON_LEFT,
            "right": mouse_buttons.MOUSEBUTTON_RIGHT,
            "middle": mouse_buttons.MOUSEBUTTON_MIDDLE,
        }
        if button not in choices:
            raise ValueError("button must be left, right, or middle")
        _unwrap(await self._require_connection().send_mouse(choices[button], x, y, pressed), "RDP mouse button")

    async def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        self.validate_point(x, y)
        if delta_x:
            raise ValueError("the RDP backend currently supports vertical scrolling only")
        if not delta_y:
            return
        notches = max(1, min(20, abs(delta_y) // 120 or 1))
        mouse_buttons = self._require_mouse_buttons()
        # aardwolf 0.2.13's WHEEL_DOWN branch omits PTRFLAGS.WHEEL. Its WHEEL_UP
        # branch masks a signed step into the protocol's 9-bit rotation field,
        # producing the correct WHEEL | WHEEL_NEGATIVE flags for negative values.
        steps = -120 if delta_y > 0 else 120
        for _ in range(notches):
            _unwrap(
                await self._require_connection().send_mouse(
                    mouse_buttons.MOUSEBUTTON_WHEEL_UP,
                    x,
                    y,
                    False,
                    steps,
                ),
                "RDP mouse wheel",
            )

    async def type_text(self, text: str) -> None:
        if len(text) > 10_000:
            raise ValueError("text is limited to 10,000 characters per call")
        connection = self._require_connection()
        utf16 = text.encode("utf-16-le")
        for index in range(0, len(utf16), 2):
            code_unit = int.from_bytes(utf16[index : index + 2], "little")
            character = _Utf16CodeUnit(code_unit) if 0xD800 <= code_unit <= 0xDFFF else chr(code_unit)
            _unwrap(await connection.send_key_char(character, True), "RDP key press")
            _unwrap(await connection.send_key_char(character, False), "RDP key release")
            await asyncio.sleep(0.001)

    async def key_event(self, key: str, pressed: bool) -> None:
        normalized = key.lower().replace("_", "")
        if normalized not in SCANCODES:
            raise ValueError(f"unsupported RDP key name: {key}")
        scancode, extended = SCANCODES[normalized]
        _unwrap(await self._require_connection().send_key_scancode(scancode, pressed, extended), "RDP key event")


    async def key_chord(self, keys: list[str]) -> None:
        if not keys or len(keys) > 8:
            raise ValueError("keypress requires between 1 and 8 keys")
        normalized = [key.strip().lower().replace("_", "") for key in keys]
        unknown = [key for key in normalized if key not in SCANCODES]
        if unknown:
            raise ValueError(f"unsupported RDP key names: {', '.join(unknown)}")
        connection = self._require_connection()
        pressed_maybe: list[str] = []
        try:
            for key in normalized:
                # Record ownership before awaiting: the server may observe the
                # key-down even if this coroutine is cancelled mid-send.
                pressed_maybe.append(key)
                scancode, extended = SCANCODES[key]
                _unwrap(await connection.send_key_scancode(scancode, True, extended), f"RDP press {key}")
            while pressed_maybe:
                key = pressed_maybe[-1]
                scancode, extended = SCANCODES[key]
                _unwrap(await connection.send_key_scancode(scancode, False, extended), f"RDP release {key}")
                pressed_maybe.pop()
        except BaseException:
            for key in reversed(pressed_maybe):
                scancode, extended = SCANCODES[key]
                try:
                    _unwrap(await connection.send_key_scancode(scancode, False, extended), f"RDP release {key}")
                except BaseException:
                    logger.warning("RDP key release after chord failure also failed", exc_info=True)
            raise

    async def release_inputs(self) -> None:
        connection = self._require_connection()
        mouse_buttons = self._require_mouse_buttons()
        errors: list[BaseException] = []
        for button in (
            mouse_buttons.MOUSEBUTTON_LEFT,
            mouse_buttons.MOUSEBUTTON_RIGHT,
            mouse_buttons.MOUSEBUTTON_MIDDLE,
        ):
            try:
                _unwrap(await connection.send_mouse(button, 0, 0, False), "RDP mouse release")
            except BaseException as error:  # noqa: BLE001 - attempt every release
                errors.append(error)
        for scancode, extended in sorted(set(SCANCODES.values())):
            try:
                _unwrap(await connection.send_key_scancode(scancode, False, extended), "RDP release key")
            except BaseException as error:  # noqa: BLE001 - attempt every release
                errors.append(error)
        if errors:
            raise RuntimeError("RDP input release failed: " + "; ".join(str(error) for error in errors))
