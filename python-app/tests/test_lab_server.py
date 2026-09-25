import asyncio
import re
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import httpx
import pytest

from app.lab_server import WorkspaceLabServer, start_guest_workspace_lab_server, start_workspace_lab_server


async def test_workspace_http_serves_nested_assets_and_blocks_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("lab")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "style.css").write_text("body {}")
    (tmp_path / "secret").write_text("secret")
    server = await start_workspace_lab_server(workspace_path=workspace)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(server.url_for())
            await asyncio.wait_for(server.wait_for_index(), 0.1)
            assert response.text == "lab"
            assert response.headers["cache-control"] == "no-store"
            assert (await client.get(server.url_for("nested/style.css"))).headers["content-type"] == "text/css"
            assert (await client.get(server.url_for("%2e%2e/secret"))).status_code == 404
            assert (await client.post(server.url_for())).status_code == 405
    finally:
        await server.close()


async def test_close_drops_an_idle_http_connection(tmp_path):
    server = await start_workspace_lab_server(workspace_path=tmp_path)
    _, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
    await asyncio.wait_for(server.close(), 1)
    writer.close()


async def test_guest_server_uses_public_host_and_configured_port(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("guest lab")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_HOST", "192.0.2.25")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_PORT", "0")

    server = await start_guest_workspace_lab_server(
        workspace_path=tmp_path,
        remote_host="192.0.2.50",
        remote_port=3389,
    )
    try:
        parsed = urlsplit(server.url_for())
        assert parsed.hostname == "192.0.2.25"
        assert parsed.port == server.server.sockets[0].getsockname()[1]
        assert re.fullmatch(r"/[0-9a-f]{32}/index.html", parsed.path)
        assert server.allowed_peers == frozenset({"192.0.2.50"})
    finally:
        await server.close()


@pytest.mark.parametrize("value", ["invalid", "-1", "65536"])
async def test_guest_server_rejects_invalid_port(tmp_path, monkeypatch, value):
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_HOST", "127.0.0.1")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_PORT", value)

    with pytest.raises(ValueError, match="0 to 65535"):
        await start_guest_workspace_lab_server(
            workspace_path=tmp_path,
            remote_host="192.0.2.50",
            remote_port=3389,
        )


async def test_capability_is_required_for_every_asset_and_readiness(tmp_path):
    (tmp_path / "index.html").write_text('<script src="nested/app.js"></script>')
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "app.js").write_text("console.log('lab')")
    server = await start_workspace_lab_server(workspace_path=tmp_path)
    other = await start_workspace_lab_server(workspace_path=tmp_path)
    try:
        parsed = urlsplit(server.url_for())
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with httpx.AsyncClient(trust_env=False) as client:
            for path in ("/", "/index.html", "/nested/app.js", "/wrong/index.html", urlsplit(other.url_for()).path):
                assert (await client.get(origin + path)).status_code == 404
                assert not server.index_requested.is_set()
            assert (await client.head(server.url_for())).status_code == 200
            assert not server.index_requested.is_set()
            assert (await client.get(server.url_for("nested/app.js"))).text == "console.log('lab')"
            assert not server.index_requested.is_set()
            response = await client.get(server.url_for())
            assert response.status_code == 200
            assert response.headers["referrer-policy"] == "no-referrer"
            await server.wait_for_index(timeout=0.2)
    finally:
        await server.close()
        await other.close()


@pytest.mark.parametrize("path", [
    "%2e%2e/secret", "%2e%2e%5csecret", "nested/%2e%2e/%2e%2e/secret",
    "C%3A/Windows/win.ini", "%5c%5chost/share", "index.html%3Astream", "%00",
])
async def test_capability_path_still_rejects_escape(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret").write_text("secret")
    server = await start_workspace_lab_server(workspace_path=workspace)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(server.url_for(path))).status_code == 404
            assert not server.index_requested.is_set()
    finally:
        await server.close()


async def test_capability_path_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret").write_text("secret")
    try:
        (workspace / "linked").symlink_to(tmp_path / "secret")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise
    server = await start_workspace_lab_server(workspace_path=workspace)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(server.url_for("linked"))).status_code == 404
    finally:
        await server.close()


@pytest.mark.parametrize("expected_peer,allowed", [("192.0.2.50", False), ("::ffff:127.0.0.1", True)])
async def test_guest_checks_socket_peer_not_forwarded_headers(tmp_path, monkeypatch, expected_peer, allowed):
    (tmp_path / "index.html").write_text("guest lab")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_HOST", "127.0.0.1")
    monkeypatch.setenv("REMOTE_COMPUTER_LAB_GUEST_HOST", expected_peer)
    server = await start_guest_workspace_lab_server(workspace_path=tmp_path, remote_host="192.0.2.1", remote_port=3389)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(server.url_for(), headers={"X-Forwarded-For": expected_peer})
            assert response.status_code == (200 if allowed else 404)
            assert server.index_requested.is_set() is allowed
    finally:
        await server.close()


async def test_failed_index_response_does_not_satisfy_readiness(tmp_path):
    (tmp_path / "index.html").write_text("lab")
    server = WorkspaceLabServer(tmp_path)
    server.respond = AsyncMock(side_effect=[ConnectionResetError("client left"), None])
    reader = asyncio.StreamReader()
    reader.feed_data(f"GET {server._prefix}index.html HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    class Writer:
        get_extra_info = staticmethod(lambda name: ("127.0.0.1", 1234))
        is_closing = staticmethod(lambda: False)
        close = Mock()

    writer = Writer()
    await server.handle(reader, writer)
    assert not server.index_requested.is_set()
    writer.close.assert_called_once()
