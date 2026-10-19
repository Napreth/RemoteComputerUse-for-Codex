from __future__ import annotations

import asyncio
import io
import ipaddress
import threading
import weakref
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from app.backends.config import RdpConfig, VncConfig
from app.backends.rdp import aardwolf as rdp_module
from app.backends.rdp.aardwolf import AardwolfAdapter, connection_url
from app.backends.rdp.backend import RDPBackend
from app.backends.vnc import remote_control as vnc_module
from app.backends.vnc.backend import VNCBackend
from app.backends.vnc.remote_control import VncRemoteControlAdapter


class FakeProtocolAdapter:
    def __init__(self) -> None:
        self.width = 64
        self.height = 48
        self.connected = True
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.screenshot_png = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture")
        self.move = AsyncMock()
        self.mouse_button = AsyncMock()
        self.scroll = AsyncMock()
        self.type_text = AsyncMock()
        self.key_chord = AsyncMock()
        self.release_inputs = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_type", "config"),
    [
        (RDPBackend, RdpConfig(host="rdp.example", port=3389, username="codex")),
        (VNCBackend, VncConfig(host="vnc.example", port=5900)),
    ],
)
async def test_first_class_backend_delegates_to_replaceable_adapter(
    backend_type: type[RDPBackend | VNCBackend],
    config: RdpConfig | VncConfig,
) -> None:
    adapter = FakeProtocolAdapter()
    backend = backend_type(lambda: adapter)  # type: ignore[arg-type]

    await backend.connect(config)  # type: ignore[arg-type]
    await backend.move(2, 3)
    await backend.disconnect()

    adapter.connect.assert_awaited_once_with(config)
    adapter.move.assert_awaited_once_with(2, 3)
    adapter.disconnect.assert_awaited_once()


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


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"), ("rdp.example", "rdp.example"), ("WIN-DEV", "win-dev"),
        ("RDP.Example.", "rdp.example."), ("bücher.example", "xn--bcher-kva.example"),
        ("faß.de", "xn--fa-hia.de"), ("straße.de", "xn--strae-oqa.de"),
        ("ς.gr", "xn--3xa.gr"), ("σ.gr", "xn--4xa.gr"),
        ("rdp_service", "rdp_service"), ("vnc_server", "vnc_server"),
        ("WIN_Host.Local.", "win_host.local."),
        ("ab--host.local", "ab--host.local"), ("xn--fa-hia.de", "xn--fa-hia.de"),
        ("::1", "::1"), ("[::1]", "::1"), ("2001:0db8::1", "2001:db8::1"),
        ("::ffff:192.0.2.1", "::ffff:c000:201"),
    ],
)
def test_rdp_real_factory_preserves_destination_and_credentials(host: str, expected: str) -> None:
    from aardwolf.commons.factory import RDPConnectionFactory
    from aardwolf.commons.iosettings import RDPIOSettings

    config = RdpConfig(host=host, port=3390, username="alice user+@é", password="p@ss:/?#%\\é", domain="LAB")
    factory = RDPConnectionFactory.from_url(connection_url(config), RDPIOSettings())
    actual = factory.target.get_hostname_or_ip()
    if ":" in expected:
        assert ipaddress.ip_address(actual) == ipaddress.ip_address(expected)
    else:
        assert actual == expected
    assert factory.target.port == 3390
    assert not factory.target.proxies
    assert factory.credential.username == config.username
    assert factory.credential.domain == config.domain
    assert factory.credential.secret == config.password


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1@attacker.example", "127.0.0.1/?serverip=attacker.example", "host?proxyhost=evil",
        "host#fragment", "host\\@evil", "rdp://host", "host:3389", "host%40evil", "host\n.evil",
        "host\x00.evil", "host name", "-host.example", "host..example", "a" * 64 + ".example",
        "999.999.999.999", "127.0.0.01", "127.1", "0x7f000001", "0x7f.0.0.1", "0177.0.0.1",
        "2130706433", "[example.com]", "[127.0.0.1]", "[::1",
        "fe80::1%40", "host＠evil", "host／evil", "host：3389",
    ],
)
def test_rdp_url_rejects_host_syntax_before_credentials_are_used(host: str) -> None:
    with pytest.raises(ValueError, match="host must") as caught:
        connection_url(RdpConfig(host=host, port=3389, username="alice", password="secret-needle"))
    assert "secret-needle" not in str(caught.value)
    assert host not in str(caught.value)


@pytest.mark.parametrize("port", [0, 65536, True, "3389@attacker.example", "3389?serverip=evil"])
def test_rdp_url_rejects_invalid_direct_config_port(port) -> None:
    with pytest.raises(ValueError, match="RDP port"):
        connection_url(RdpConfig(host="127.0.0.1", port=port, username="alice", password="secret-needle"))


@pytest.mark.asyncio
async def test_rdp_text_uses_utf16_units_including_surrogate_pairs() -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_char=AsyncMock(return_value=(True, None)),
    )
    backend = AardwolfAdapter()
    backend._connection = connection

    await backend.type_text("你😀")

    encoded = [call.args[0].encode("utf-16-le") for call in connection.send_key_char.await_args_list[::2]]
    assert encoded == ["你".encode("utf-16-le"), b"=\xd8", b"\x00\xde"]


@pytest.mark.asyncio
async def test_rdp_string_typing_paces_each_event_without_changing_utf16_order(monkeypatch):
    events = []

    async def send_char(character, pressed):
        events.append(("unicode", int.from_bytes(character.encode("utf-16-le"), "little"), pressed))
        return True, None

    async def send_scan(scancode, pressed, extended):
        events.append(("scan", scancode, pressed))
        assert not extended
        return True, None

    async def pause(seconds):
        events.append(("wait", seconds))

    monkeypatch.setattr(rdp_module.asyncio, "sleep", pause)
    adapter = AardwolfAdapter()
    adapter._connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_char=send_char, send_key_scancode=send_scan,
    )
    await adapter.type_text("a你😀\t\n\b\r")
    expected = []
    for kind, value in (("unicode", ord("a")), ("unicode", ord("你")),
                        ("unicode", 0xD83D), ("unicode", 0xDE00),
                        ("scan", 0x0F), ("scan", 0x1C), ("scan", 0x0E)):
        expected += [(kind, value, True), ("wait", 0.1), (kind, value, False), ("wait", 0.1)]
    assert events == expected
    events.clear()
    await adapter.type_text("")
    await adapter.type_text("\r")
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["你", "😀", "\t"])
async def test_rdp_cancellation_during_text_pacing_preserves_release_identities(monkeypatch, text):
    frames = []
    monkeypatch.setattr("app.input_tracking._observer", lambda protocol, values: frames.append((protocol, values)))
    sleeping = asyncio.Event()

    async def pause(seconds):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rdp_module.asyncio, "sleep", pause)
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_char=AsyncMock(return_value=(True, None)),
        send_key_scancode=AsyncMock(return_value=(True, None)),
        send_mouse=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection
    adapter._mouse_buttons = SimpleNamespace(MOUSEBUTTON_LEFT=1, MOUSEBUTTON_RIGHT=2, MOUSEBUTTON_MIDDLE=3)
    task = asyncio.create_task(adapter.type_text(text))
    await asyncio.wait_for(sleeping.wait(), 1)
    # The parent already knows about the input before the newly added cancel
    # point, so an interrupted key-down can still be released by the helper.
    assert frames
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.release_inputs()
    if text == "\t":
        assert frames == [("rdp", [])]
        assert (0x0F, False, False) in [call.args for call in connection.send_key_scancode.await_args_list]
    else:
        encoded = text.encode("utf-16-le")
        identities = {int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)}
        assert adapter._input_tracker.identities == identities
        released = {int.from_bytes(call.args[0].encode("utf-16-le"), "little")
                    for call in connection.send_key_char.await_args_list if call.args[1] is False}
        assert released == identities


@pytest.mark.asyncio
async def test_rdp_string_typing_uses_scancodes_for_control_literals() -> None:
    events: list[tuple[str, object, bool]] = []

    async def send_key_char(character: object, pressed: bool) -> tuple[bool, None]:
        events.append(("unicode", character, pressed))
        return True, None

    async def send_key_scancode(scancode: int, pressed: bool, extended: bool) -> tuple[bool, None]:
        events.append(("scan", scancode, pressed))
        assert extended is False
        return True, None

    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_char=send_key_char,
        send_key_scancode=send_key_scancode,
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.type_text("a\t\n\r\bb")

    assert events == [
        ("unicode", "a", True), ("unicode", "a", False),
        ("scan", 0x0F, True), ("scan", 0x0F, False),
        ("scan", 0x1C, True), ("scan", 0x1C, False),
        ("scan", 0x0E, True), ("scan", 0x0E, False),
        ("unicode", "b", True), ("unicode", "b", False),
    ]
    assert adapter._input_tracker.identities == {ord("a"), ord("b")}


@pytest.mark.asyncio
async def test_rdp_control_only_typing_reports_risk_before_blocked_keydown(monkeypatch: pytest.MonkeyPatch) -> None:
    frames: list[tuple[str, list[int]]] = []
    monkeypatch.setattr("app.input_tracking._observer", lambda protocol, values: frames.append((protocol, values)))
    down_started = asyncio.Event()

    async def send_key_scancode(scancode: int, pressed: bool, extended: bool) -> tuple[bool, None]:
        assert (scancode, pressed, extended) == (0x0F, True, False)
        down_started.set()
        await asyncio.Event().wait()
        return True, None

    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=send_key_scancode,
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection
    await adapter.type_text("")
    assert frames == []

    task = asyncio.create_task(adapter.type_text("\t"))
    await asyncio.wait_for(down_started.wait(), 1)
    assert frames == [("rdp", [])]
    assert adapter._input_tracker.identities == set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    connection.send_key_scancode = AsyncMock(return_value=(True, None))
    connection.send_mouse = AsyncMock(return_value=(True, None))
    adapter._mouse_buttons = SimpleNamespace(MOUSEBUTTON_LEFT=1, MOUSEBUTTON_RIGHT=2, MOUSEBUTTON_MIDDLE=3)
    await adapter.release_inputs()
    assert (0x0F, False, False) in [call.args for call in connection.send_key_scancode.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ctrl", "A"])
async def test_rdp_chord_cancellation_releases_key_down(key: str) -> None:
    down_delivered = asyncio.Event()
    events: list[tuple[int, bool, bool]] = []

    async def send_key_scancode(scancode: int, pressed: bool, extended: bool) -> tuple[bool, None]:
        events.append((scancode, pressed, extended))
        if pressed and len(events) == 1:
            down_delivered.set()
            await asyncio.Event().wait()
        return True, None

    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=send_key_scancode,
    )
    backend = AardwolfAdapter()
    backend._connection = connection
    task = asyncio.create_task(backend.key_chord([key]))
    await down_delivered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-1][1] is False
    assert events[-1][0] == events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "base_code"), [("A", 0x1E), ("!", 0x02)])
async def test_rdp_character_events_use_shifted_scancodes(key: str, base_code: int) -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_event(key, True)
    await adapter.key_event(key, False)

    assert [call.args for call in connection.send_key_scancode.await_args_list] == [
        (0x2A, True, False), (base_code, True, False), (0x2A, False, False),
        (0x2A, True, False), (base_code, False, False), (0x2A, False, False),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("keys", "base_code"), [(["A"], 0x1E), (["!"], 0x02)])
async def test_rdp_character_chord_preserves_shift(keys: list[str], base_code: int) -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_chord(keys)

    assert [call.args for call in connection.send_key_scancode.await_args_list] == [
        (0x2A, True, False), (base_code, True, False), (0x2A, False, False),
        (0x2A, True, False), (base_code, False, False), (0x2A, False, False),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("literal", "scancode"), [
    ("\t", 0x0F), ("\n", 0x1C), ("\b", 0x0E),
])
async def test_rdp_control_key_literals_send_named_scancodes(literal: str, scancode: int) -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_chord([literal])
    await adapter.key_event(literal, True)
    await adapter.key_event(literal, False)

    assert [call.args for call in connection.send_key_scancode.await_args_list] == [
        (scancode, True, False), (scancode, False, False),
        (scancode, True, False), (scancode, False, False),
    ]


@pytest.mark.asyncio
async def test_rdp_carriage_return_is_noop_for_keys_and_string_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    frames: list[tuple[str, list[int]]] = []
    monkeypatch.setattr("app.input_tracking._observer", lambda protocol, values: frames.append((protocol, values)))
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
        send_key_char=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_chord(["\r"])
    await adapter.key_event("\r", True)
    await adapter.key_event("\r", False)
    await adapter.type_text("\r")

    connection.send_key_scancode.assert_not_awaited()
    connection.send_key_char.assert_not_awaited()
    assert adapter._input_tracker.identities == set()
    assert frames == []


@pytest.mark.asyncio
async def test_rdp_character_shift_does_not_leak_into_intervening_keypress() -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_event("A", True)
    await adapter.key_chord(["b"])
    await adapter.key_event("A", False)

    assert [call.args for call in connection.send_key_scancode.await_args_list] == [
        (0x2A, True, False), (0x1E, True, False), (0x2A, False, False),
        (0x30, True, False), (0x30, False, False),
        (0x2A, True, False), (0x1E, False, False), (0x2A, False, False),
    ]


@pytest.mark.asyncio
async def test_rdp_ctrl_uppercase_chord_keeps_ctrl_until_character_release() -> None:
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=AsyncMock(return_value=(True, None)),
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    await adapter.key_event("ctrl", True)
    await adapter.key_event("A", True)
    await adapter.key_event("A", False)
    await adapter.key_event("ctrl", False)

    assert [call.args for call in connection.send_key_scancode.await_args_list] == [
        (0x1D, True, False), (0x2A, True, False), (0x1E, True, False), (0x2A, False, False),
        (0x2A, True, False), (0x1E, False, False), (0x2A, False, False), (0x1D, False, False),
    ]


@pytest.mark.asyncio
async def test_rdp_character_failure_still_attempts_shift_release() -> None:
    events: list[tuple[int, bool, bool]] = []

    async def send_key_scancode(scancode: int, pressed: bool, extended: bool) -> tuple[bool, None]:
        events.append((scancode, pressed, extended))
        if scancode == 0x1E and pressed:
            raise RuntimeError("character press failed")
        return True, None

    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_key_scancode=send_key_scancode,
    )
    adapter = AardwolfAdapter()
    adapter._connection = connection

    with pytest.raises(RuntimeError, match="character press failed"):
        await adapter.key_event("A", True)

    assert events == [
        (0x2A, True, False), (0x1E, True, False), (0x2A, False, False),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("delta", "steps"), [(120, -120), (-120, 120)])
async def test_rdp_scroll_uses_valid_signed_wheel_frame(delta: int, steps: int) -> None:
    wheel_up = object()
    connection = SimpleNamespace(
        disconnected_evt=SimpleNamespace(is_set=lambda: False),
        send_mouse=AsyncMock(return_value=(True, None)),
    )
    backend = AardwolfAdapter()
    backend.width = 100
    backend.height = 80
    backend._connection = connection
    backend._mouse_buttons = SimpleNamespace(MOUSEBUTTON_WHEEL_UP=wheel_up)

    await backend.scroll(10, 20, 0, delta)

    connection.send_mouse.assert_awaited_once_with(wheel_up, 10, 20, False, steps)


@pytest.fixture
def rdp_frame_stream(monkeypatch: pytest.MonkeyPatch):
    from aardwolf.commons.queuedata.video import RDP_VIDEO
    from aardwolf.connection import RDPConnection
    from aardwolf.protocol.fastpath import FASTPATH_FRAGMENT, FASTPATH_UPDATETYPE
    from aardwolf.protocol.fastpath.bitmap import TS_BITMAP_DATA, TS_BITMAP_FLAG
    from PIL import Image

    updates = weakref.WeakValueDictionary()
    images = weakref.WeakValueDictionary()
    decode = RDP_VIDEO.from_bitmapdata

    def tracked_decode(*args):
        update, image = decode(*args)
        updates[id(update)] = update
        images[id(image)] = image
        return update, image

    async def render(connection, color, x=0, y=0):
        bitmap = TS_BITMAP_DATA()
        bitmap.destLeft, bitmap.destTop = x, y
        bitmap.width = bitmap.height = 32
        bitmap.destRight, bitmap.destBottom = x + 31, y + 31
        bitmap.bitsPerPixel, bitmap.flags = 32, TS_BITMAP_FLAG(0)
        bitmap.bitmapDataStream = bytes((*color, 255)) * (32 * 32)
        pdu = SimpleNamespace(fpOutputUpdates=SimpleNamespace(
            fragmentation=FASTPATH_FRAGMENT.SINGLE,
            updateCode=FASTPATH_UPDATETYPE.BITMAP,
            update=SimpleNamespace(rectangles=[bitmap]),
        ))
        # Run the pinned library's actual decoding, compositing and queue write.
        await connection._RDPConnection__process_fastpath(pdu)

    stream = SimpleNamespace(
        connections=[], outcome="success", updates=updates, images=images, render=render,
    )

    async def connect(connection):
        stream.connections.append(connection)
        connection._RDPConnection__desktop_buffer = Image.new("RGBA", (64, 48))
        connection._RDPConnection__joined_channels = {}
        # Frames can arrive during the handshake, before the adapter is published.
        await render(connection, (20, 40, 60))
        if stream.outcome == "failure":
            raise RuntimeError("handshake failed after receiving a frame")
        if stream.outcome == "cancelled":
            raise asyncio.CancelledError()
        if stream.outcome == "timeout":
            await asyncio.Event().wait()
        return True, None

    monkeypatch.setattr(RDP_VIDEO, "from_bitmapdata", staticmethod(tracked_decode))
    # Only the network handshake is replaced; use the real factory, decoder,
    # framebuffer, PNG encoder and connection termination code.
    monkeypatch.setattr(RDPConnection, "connect", connect)
    monkeypatch.setattr(RDPConnection, "send_disconnect", AsyncMock(return_value=(True, None)))
    return stream


@pytest.mark.asyncio
async def test_rdp_frame_updates_are_not_retained_and_screenshots_stay_current(rdp_frame_stream):
    from PIL import Image

    adapter = AardwolfAdapter()
    await adapter.connect(RdpConfig(host="target", port=3389, username="user", width=64, height=48))
    connection = rdp_frame_stream.connections[0]
    try:
        assert not rdp_frame_stream.updates
        assert not rdp_frame_stream.images
        # Feed bursts without sleeping or taking screenshots. Discarding output
        # must neither retain the burst nor block the library's receive path.
        for batch in range(20):
            for index in range(100):
                await rdp_frame_stream.render(connection, (batch, index, 120), 32, 16)
            assert not rdp_frame_stream.updates
            assert not rdp_frame_stream.images
        with Image.open(io.BytesIO(await adapter.screenshot_png())) as screenshot:
            assert screenshot.size == (64, 48)
            assert screenshot.getpixel((0, 0))[:3] == (20, 40, 60)
            assert screenshot.getpixel((63, 47))[:3] == (19, 99, 120)
    finally:
        # The real terminate() writes its None sentinel to the same output.
        await asyncio.wait_for(adapter.disconnect(), timeout=1)
    assert connection.disconnected_evt.is_set()
    assert not rdp_frame_stream.updates
    assert not rdp_frame_stream.images


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "error"),
    [("failure", RuntimeError), ("cancelled", asyncio.CancelledError), ("timeout", TimeoutError)],
)
async def test_rdp_frames_received_during_failed_startup_are_not_retained(rdp_frame_stream, outcome, error):
    rdp_frame_stream.outcome = outcome
    adapter = AardwolfAdapter()
    timeout = 0.1 if outcome == "timeout" else 2.0
    with pytest.raises(error):
        await adapter.connect(RdpConfig(host="target", port=3389, username="user", connect_timeout=timeout))
    assert not adapter.connected
    assert rdp_frame_stream.connections[0].disconnected_evt.is_set()
    assert not rdp_frame_stream.updates
    assert not rdp_frame_stream.images


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
    backend = AardwolfAdapter()

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
    backend = AardwolfAdapter()

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
    backend = AardwolfAdapter()
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
    backend = VncRemoteControlAdapter()
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
async def test_vnc_text_controls_use_named_x11_keysyms() -> None:
    taps: list[int] = []
    client = SimpleNamespace(
        _sock=SimpleNamespace(fileno=lambda: 7),
        tap=taps.append,
    )
    adapter = VncRemoteControlAdapter()
    adapter._client = client

    await adapter.type_text("\t\n\r\b")

    assert taps == [0xFF09, 0xFF0D, 0xFF0D, 0xFF08]
    assert adapter._input_tracker.identities == set(taps)
    with pytest.raises(ValueError, match="unsupported VNC text control"):
        await adapter.type_text("\x01")
    assert taps == [0xFF09, 0xFF0D, 0xFF0D, 0xFF08]


def test_vnc_held_key_identity_uses_keysym_aliases_without_folding_character_case() -> None:
    backend = VNCBackend()
    assert backend.key_identity("escape") == backend.key_identity("esc") == 0xFF1B
    assert backend.key_identity("A") != backend.key_identity("a")


@pytest.mark.asyncio
async def test_vnc_connect_failure_closes_unpublished_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRfbClient(FakeRfbClient):
        def connect(self) -> None:
            raise OSError("connection refused")

    FailingRfbClient.instances.clear()
    monkeypatch.setattr(vnc_module, "RfbClient", FailingRfbClient)
    backend = VncRemoteControlAdapter()

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
    backend = VncRemoteControlAdapter()
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
    backend = VncRemoteControlAdapter()

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
    backend = VncRemoteControlAdapter()
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
    backend = VncRemoteControlAdapter()
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
    backend = AardwolfAdapter()
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
    backend = VncRemoteControlAdapter()
    backend._client = client
    await backend.key_event("A", True)
    assert events == [(ord("A"), True)]
    await backend.release_inputs()
    assert (ord("A"), False) in events
    with pytest.raises(ValueError, match="printable ASCII"):
        await backend.key_event("你", True)


@pytest.mark.asyncio
async def test_rdp_unicode_release_uses_original_utf16_units_after_failed_up():
    from app.input_tracking import seed_release
    events = []
    async def send_char(character, pressed):
        value = int.from_bytes(character.encode("utf-16-le"), "little")
        events.append((value, pressed))
        if not pressed:
            raise RuntimeError("keyup lost")
        return True, None
    connection = SimpleNamespace(disconnected_evt=SimpleNamespace(is_set=lambda: False), send_key_char=send_char)
    adapter = AardwolfAdapter()
    adapter._connection = connection
    with pytest.raises(RuntimeError, match="keyup lost"):
        await adapter.type_text("你😀")
    identities = adapter._input_tracker.identities
    assert identities == {ord("你"), 0xD83D, 0xDE00}
    # A fresh helper adapter receives the original worker's identities.
    seed_release("rdp", list(identities))
    try:
        helper = AardwolfAdapter()
    finally:
        seed_release("rdp", [])
    connection.send_key_char = AsyncMock(return_value=(True, None))
    connection.send_key_scancode = AsyncMock(return_value=(True, None))
    connection.send_mouse = AsyncMock(return_value=(True, None))
    helper._connection = connection
    helper._mouse_buttons = SimpleNamespace(MOUSEBUTTON_LEFT=1, MOUSEBUTTON_RIGHT=2, MOUSEBUTTON_MIDDLE=3)
    await helper.release_inputs()
    assert {int.from_bytes(call.args[0].encode("utf-16-le"), "little")
            for call in connection.send_key_char.await_args_list} == identities
    assert all(call.args[1] is False for call in connection.send_key_char.await_args_list)


@pytest.mark.asyncio
async def test_vnc_release_includes_unicode_identity():
    from app.input_tracking import seed_release
    symbol = 0x01000000 | ord("你")
    seed_release("vnc", [symbol])
    try:
        helper = VncRemoteControlAdapter()
    finally:
        seed_release("vnc", [])
    events = []
    helper._client = SimpleNamespace(_sock=SimpleNamespace(fileno=lambda: 7),
                                     _key=lambda key, *, down: events.append((key, down)),
                                     _pointer=lambda *args: None)
    await helper.release_inputs()
    assert (symbol, False) in events


@pytest.mark.parametrize("protocol,values", [("rdp", [65536]), ("vnc", [0x10000]), ("vnc", [True])])
def test_invalid_input_identities_cannot_be_acknowledged(protocol, values):
    from app.input_tracking import validate_identities
    with pytest.raises(ValueError, match="release cannot be confirmed"):
        validate_identities(protocol, values)


@pytest.mark.asyncio
async def test_unicode_input_is_not_sent_when_identity_reporting_fails(monkeypatch):
    from app import input_tracking
    def failed_report(protocol, values):
        raise BrokenPipeError("parent is gone")
    monkeypatch.setattr(input_tracking, "_observer", failed_report)
    connection = SimpleNamespace(disconnected_evt=SimpleNamespace(is_set=lambda: False),
                                 send_key_char=AsyncMock(return_value=(True, None)))
    backend = AardwolfAdapter()
    backend._connection = connection
    with pytest.raises(BrokenPipeError, match="parent is gone"):
        await backend.type_text("你")
    connection.send_key_char.assert_not_awaited()
