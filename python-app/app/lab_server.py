"""Serve a run-scoped lab behind a capability path and optional peer restriction."""
import asyncio
import ipaddress
import mimetypes
import os
import secrets
import socket
from pathlib import Path
from urllib.parse import unquote, urlsplit


def normalized_address(value: str) -> str:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


class WorkspaceLabServer:
    def __init__(self, root: Path, public_host: str | None = None, allowed_peers: frozenset[str] | None = None):
        self.root = root.resolve()
        self.public_host = public_host
        self._prefix = f"/{secrets.token_hex(16)}/"
        self.allowed_peers = (
            frozenset(normalized_address(peer) for peer in allowed_peers) if allowed_peers is not None else None
        )
        self.server: asyncio.Server | None = None
        self.connections: set[asyncio.StreamWriter] = set()
        self.tasks: set[asyncio.Task] = set()
        self.index_requested = asyncio.Event()
        self.closing = False

    def url_for(self, path: str = "index.html") -> str:
        assert self.server and self.server.sockets
        address = self.public_host or str(self.server.sockets[0].getsockname()[0])
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        return f"http://{host}:{self.server.sockets[0].getsockname()[1]}{self._prefix}{path.lstrip('/')}"

    async def wait_for_index(self, timeout: float = 20.0) -> None:
        await asyncio.wait_for(self.index_requested.wait(), timeout)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closing:
            writer.close()
            return
        self.connections.add(writer)
        task = asyncio.current_task()
        if task:
            self.tasks.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            peer = writer.get_extra_info("peername")
            if self.allowed_peers is not None and (
                not peer or normalized_address(peer[0]) not in self.allowed_peers
            ):
                raise FileNotFoundError("Lab asset not found")
            method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            if method not in ("GET", "HEAD"):
                await self.respond(writer, 405, b"Method not allowed", "text/plain")
                return
            path = unquote(urlsplit(target).path)
            if not path.startswith(self._prefix):
                raise FileNotFoundError("Lab asset not found")
            relative = path[len(self._prefix):] or "index.html"
            # Keep decoded URL paths portable: reject traversal, Windows drive
            # paths, backslashes, and empty segments before touching the disk.
            if "\\" in relative or ":" in relative or any(part in ("", ".", "..") for part in relative.split("/")):
                raise FileNotFoundError("Lab asset not found")
            asset = (self.root / relative).resolve()
            if not asset.is_relative_to(self.root) or not asset.is_file():
                raise FileNotFoundError(path)
            payload = await asyncio.to_thread(asset.read_bytes)
            mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            await self.respond(writer, 200, payload, mime, head=method == "HEAD")
            if relative == "index.html" and method == "GET":
                self.index_requested.set()
        except (TimeoutError, ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not writer.is_closing():
                await self.respond(writer, 404, b'{"error":"Lab asset not found"}', "application/json")
        finally:
            writer.close()
            self.connections.discard(writer)
            if task:
                self.tasks.discard(task)

    async def respond(self, writer: asyncio.StreamWriter, status: int, body: bytes, mime: str, *, head: bool = False) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}[status]
        writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\n"
                      "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                      "Connection: close\r\n\r\n").encode("ascii"))
        if not head:
            writer.write(body)
        await writer.drain()

    async def close(self) -> None:
        self.closing = True
        if self.server:
            self.server.close()
        for writer in list(self.connections):
            writer.close()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.server:
            await asyncio.wait_for(self.server.wait_closed(), 1)


def local_address_for_remote(remote_host: str, remote_port: int) -> str:
    """Return the host interface address selected by the route to a guest."""
    errors: list[OSError] = []
    for family, socket_type, protocol, _, address in socket.getaddrinfo(
        remote_host,
        remote_port,
        type=socket.SOCK_DGRAM,
    ):
        transport = socket.socket(family, socket_type, protocol)
        try:
            transport.connect(address)
            return str(transport.getsockname()[0])
        except OSError as error:
            errors.append(error)
        finally:
            transport.close()
    detail = f": {errors[-1]}" if errors else ""
    raise RuntimeError(f"could not determine a host address reachable from {remote_host}{detail}")


async def start_workspace_lab_server(
    *,
    workspace_path: Path | str,
    bind_host: str = "127.0.0.1",
    public_host: str | None = None,
    port: int = 0,
    allowed_peers: frozenset[str] | None = None,
) -> WorkspaceLabServer:
    server = WorkspaceLabServer(Path(workspace_path), public_host, allowed_peers)
    server.server = await asyncio.start_server(server.handle, bind_host, port, limit=8192)
    return server


async def start_guest_workspace_lab_server(
    *,
    workspace_path: Path | str,
    remote_host: str,
    remote_port: int,
) -> WorkspaceLabServer:
    public_host = os.getenv("REMOTE_COMPUTER_LAB_HOST") or local_address_for_remote(
        remote_host,
        remote_port,
    )
    bind_host = os.getenv("REMOTE_COMPUTER_LAB_BIND_HOST") or public_host
    configured_port = os.getenv("REMOTE_COMPUTER_LAB_PORT", "0")
    try:
        port = int(configured_port)
    except ValueError as error:
        raise ValueError("REMOTE_COMPUTER_LAB_PORT must be an integer from 0 to 65535") from error
    if not 0 <= port <= 65_535:
        raise ValueError("REMOTE_COMPUTER_LAB_PORT must be an integer from 0 to 65535")
    # A forwarded RDP/VNC endpoint may differ from the guest's HTTP source.
    # Resolve the explicit guest override when present, otherwise the target.
    guest_host = os.getenv("REMOTE_COMPUTER_LAB_GUEST_HOST") or remote_host
    addresses = await asyncio.get_running_loop().getaddrinfo(guest_host, remote_port, type=socket.SOCK_STREAM)
    allowed_peers = frozenset(normalized_address(str(address[4][0])) for address in addresses)
    if not allowed_peers:
        raise ValueError("could not resolve the lab guest's source address")
    return await start_workspace_lab_server(
        workspace_path=workspace_path,
        bind_host=bind_host,
        public_host=public_host,
        port=port,
        allowed_peers=allowed_peers,
    )
