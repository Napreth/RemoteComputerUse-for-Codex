from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from app.backends.config import RdpConfig, VncConfig
from app.backends.rdp import backend as rdp_module
from app.backends.rdp.backend import RDPBackend, connection_url
from app.backends.vnc import backend as vnc_module
from app.backends.vnc.backend import VNCBackend


def test_rdp_url_encodes_credentials_and_ipv6() -> None:
    config = RdpConfig(
        host="2001:db8::1",
        port=3389,
        username="codex user",
        password="p@ss:word",
        domain="LAB",
    )
    assert connection_url(config) == (
        "rdp+ntlm-password://LAB\\codex%20user:p%40ss%3Aword@[2001:db8::1]:3389"
    )


@pytest.mark.asyncio
async def test_rdp_text_uses_utf16_units_including_surrogate_pairs() -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_char=AsyncMock(return_value=(True, None)),
    )
    backend = RDPBackend()
    backend._connection = connection

    await backend.type_text("你😀")

    encoded = [call.args[0].encode("utf-16-le") for call in connection.send_key_char.await_args_list[::2]]
    assert encoded == ["你".encode("utf-16-le"), b"=\xd8", b"\x00\xde"]


@pytest.mark.asyncio
async def test_rdp_chord_cancellation_releases_key_down() -> None:
    down_delivered = asyncio.Event()
    events: list[tuple[int, bool, bool]] = []

    async def send_key_scancode(scancode: int, pressed: bool, extended: bool) -> tuple[bool, None]:
        events.append((scancode, pressed, extended))
        if pressed:
            down_delivered.set()
            await asyncio.Event().wait()
        return True, None

    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=send_key_scancode,
    )
    backend = RDPBackend()
    backend._connection = connection
    task = asyncio.create_task(backend.key_chord(["ctrl", "a"]))
    await down_delivered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-1][1] is False
    assert events[-1][0] == events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("delta", "steps"), [(120, -120), (-120, 120)])
async def test_rdp_scroll_uses_valid_signed_wheel_frame(delta: int, steps: int) -> None:
    wheel_up = object()
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_mouse=AsyncMock(return_value=(True, None)),
    )
    backend = RDPBackend()
    backend.width = 100
    backend.height = 80
    backend._connection = connection
    backend._mouse_buttons = SimpleNamespace(MOUSEBUTTON_WHEEL_UP=wheel_up)

    await backend.scroll(10, 20, 0, delta)

    connection.send_mouse.assert_awaited_once_with(wheel_up, 10, 20, False, steps)


@pytest.mark.asyncio
async def test_rdp_connect_failure_terminates_unpublished_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from aardwolf.commons.factory import RDPConnectionFactory

    connection = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError("handshake failed")),
        terminate=AsyncMock(return_value=(True, None)),
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
    )
    factory = SimpleNamespace(
        target=SimpleNamespace(unsafe_ssl=False),
        get_connection=lambda settings: connection,
    )
    monkeypatch.setattr(RDPConnectionFactory, "from_url", staticmethod(lambda url, settings: factory))
    backend = RDPBackend()

    with pytest.raises(RuntimeError, match="handshake failed"):
        await backend.connect(RdpConfig(host="target", port=3389, username="user"))

    connection.terminate.assert_awaited_once()
    assert backend.connected is False


@pytest.mark.asyncio
async def test_rdp_connect_timeout_cancels_handshake_and_terminates_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aardwolf.commons.factory import RDPConnectionFactory

    handshake_cancelled = asyncio.Event()

    async def connect() -> tuple[bool, None]:
        try:
            await asyncio.Event().wait()
        finally:
            handshake_cancelled.set()
        return True, None

    connection = SimpleNamespace(
        connect=connect,
        terminate=AsyncMock(return_value=(True, None)),
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
    )
    factory = SimpleNamespace(
        target=SimpleNamespace(unsafe_ssl=False),
        get_connection=lambda settings: connection,
    )
    monkeypatch.setattr(RDPConnectionFactory, "from_url", staticmethod(lambda url, settings: factory))
    backend = RDPBackend()

    with pytest.raises(TimeoutError):
        await backend.connect(
            RdpConfig(host="target", port=3389, username="user", connect_timeout=0.01)
        )

    await asyncio.wait_for(handshake_cancelled.wait(), 0.1)
    connection.terminate.assert_awaited_once()
    assert backend.connected is False


@pytest.mark.asyncio
async def test_rdp_disconnect_timeout_hard_closes_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    never = asyncio.Event()

    async def terminate() -> None:
        await never.wait()

    transport = SimpleNamespace(close=AsyncMock())
    connection = SimpleNamespace(terminate=terminate, disconnected_evt=asyncio.Event())
    connection._RDPConnection__connection = transport
    backend = RDPBackend()
    backend._connection = connection
    monkeypatch.setattr(rdp_module, "_RDP_TERMINATE_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError, match="transport was closed"):
        await backend.disconnect()

    transport.close.assert_awaited_once()
    assert backend.connected is False


class FakeRfbClient:
    instances: ClassVar[list[FakeRfbClient]] = []

    def __init__(self, host: str, port: int, **options: Any) -> None:
        self.host = host
        self.port = port
        self.options = options
        self.width = 4
        self.height = 3
        self.events: list[tuple[Any, ...]] = []
        self._sock = SimpleNamespace(fileno=lambda: 1)
        self.instances.append(self)

    def connect(self) -> None:
        self.events.append(("connect",))

    def close(self) -> None:
        self.events.append(("close",))
        self._sock = None

    def capture(self) -> bytearray:
        return bytearray([20, 40, 60] * self.width * self.height)

    def _pointer(self, mask: int, x: int, y: int) -> None:
        self.events.append(("pointer", mask, x, y))

    def tap(self, symbol: int) -> None:
        self.events.append(("tap", symbol))

    def _key(self, symbol: int, *, down: bool) -> None:
        self.events.append(("key", symbol, down))


@pytest.mark.asyncio
async def test_vnc_backend_supports_framebuffer_pointer_wheel_and_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", FakeRfbClient)
    backend = VNCBackend()
    await backend.connect(VncConfig(host="vnc.example", port=5901, password="secret"))
    client = FakeRfbClient.instances[-1]

    png = await backend.screenshot_png()
    await backend.move(1, 2)
    await backend.mouse_button(1, 2, "right", True)
    await backend.mouse_button(1, 2, "right", False)
    await backend.scroll(1, 2, 0, 120)
    await backend.key_chord(["ctrl", "l"])
    await backend.type_text("你")
    await backend.disconnect()

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert ("pointer", 4, 1, 2) in client.events
    assert ("pointer", 16, 1, 2) in client.events
    assert ("key", 0xFFE3, True) in client.events
    assert ("tap", 0x01000000 | ord("你")) in client.events
    assert client.events[-1] == ("close",)


@pytest.mark.asyncio
async def test_vnc_connect_failure_closes_unpublished_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRfbClient(FakeRfbClient):
        def connect(self) -> None:
            raise OSError("connection refused")

    FailingRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", FailingRfbClient)
    backend = VNCBackend()

    with pytest.raises(OSError, match="connection refused"):
        await backend.connect(VncConfig(host="vnc.example", port=5901))

    assert FailingRfbClient.instances[-1].events[-1] == ("close",)
    assert backend.connected is False


@pytest.mark.asyncio
async def test_vnc_connect_cancellation_closes_socket_while_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingRfbClient(FakeRfbClient):
        def connect(self) -> None:
            started.set()
            release.wait(1)

    BlockingRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", BlockingRfbClient)
    backend = VNCBackend()
    task = asyncio.create_task(backend.connect(VncConfig(host="vnc.example", port=5901)))
    assert await asyncio.to_thread(started.wait, 0.5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    client = BlockingRfbClient.instances[-1]
    assert ("close",) in client.events
    assert backend.connected is False
    release.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_vnc_connect_has_total_deadline_and_closes_slow_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowRfbClient(FakeRfbClient):
        def connect(self) -> None:
            started.set()
            release.wait(1)

    SlowRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", SlowRfbClient)
    monkeypatch.setattr(vnc_module, "_VNC_CONNECT_GRACE", 0.01)
    backend = VNCBackend()

    with pytest.raises(TimeoutError):
        await backend.connect(VncConfig(host="vnc.example", port=5901, connect_timeout=0.1))

    client = SlowRfbClient.instances[-1]
    assert started.is_set()
    assert ("close",) in client.events
    assert backend.connected is False
    release.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_vnc_transport_failure_marks_backend_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRfbClient(FakeRfbClient):
        def capture(self) -> bytearray:
            raise OSError("server closed")

    FailingRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", FailingRfbClient)
    backend = VNCBackend()
    await backend.connect(VncConfig(host="vnc.example", port=5901))

    with pytest.raises(OSError, match="server closed"):
        await backend.screenshot_png()

    assert backend.connected is False
    assert FailingRfbClient.instances[-1].events[-1] == ("close",)


def test_vnc_connected_is_passive_and_never_reads_socket() -> None:
    class ReadGuardSocket:
        descriptor = 42

        def fileno(self) -> int:
            return self.descriptor

        def recv(self, *args: Any) -> bytes:
            raise AssertionError("connected must not read from the VNC socket")

    transport = ReadGuardSocket()
    backend = VNCBackend()
    backend._client = SimpleNamespace(_sock=transport)  # type: ignore[assignment]

    assert backend.connected is True
    transport.descriptor = -1
    assert backend.connected is False


@pytest.mark.asyncio
async def test_raw_key_event_and_cleanup_cover_held_non_modifier_keys() -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
        send_mouse=AsyncMock(return_value=(True, None)),
    )
    backend = RDPBackend()
    backend._connection = connection
    backend._mouse_buttons = SimpleNamespace(
        MOUSEBUTTON_LEFT=1, MOUSEBUTTON_RIGHT=2, MOUSEBUTTON_MIDDLE=3,
    )
    await backend.key_event("a", True)
    pressed = connection.send_key_scancode.await_args.args
    assert pressed[1] is True
    await backend.release_inputs()
    assert (pressed[0], False, pressed[2]) in [
        call.args for call in connection.send_key_scancode.await_args_list
    ]
    with pytest.raises(ValueError, match="unsupported RDP key"):
        await backend.key_event("unsupported-key", True)


@pytest.mark.asyncio
async def test_vnc_raw_key_event_and_cleanup_cover_printable_keys() -> None:
    events = []
    client = SimpleNamespace(
        _sock=SimpleNamespace(fileno=lambda: 7),
        _key=lambda key, *, down: events.append((key, down)),
        _pointer=lambda *args: None,
    )
    backend = VNCBackend()
    backend._client = client
    await backend.key_event("A", True)
    assert events == [(ord("A"), True)]
    await backend.release_inputs()
    assert (ord("A"), False) in events
    with pytest.raises(ValueError, match="printable ASCII"):
        await backend.key_event("你", True)
