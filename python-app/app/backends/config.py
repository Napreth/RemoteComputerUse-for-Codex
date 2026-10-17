"""Separate configuration models for the RDP and VNC backends."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal

import idna
from pydantic import Field

BackendKind = Literal["rdp", "vnc"]

MIN_PORT = 1
MAX_PORT = 65535
MIN_RDP_WIDTH = 320
MIN_RDP_HEIGHT = 200
MAX_RDP_DIMENSION = 8192
DEFAULT_RDP_PORT = 3389
DEFAULT_VNC_PORT = 5900
DEFAULT_RDP_WIDTH = 1440
DEFAULT_RDP_HEIGHT = 900

Port = Annotated[int, Field(strict=True, ge=MIN_PORT, le=MAX_PORT)]
RdpWidth = Annotated[int, Field(strict=True, ge=MIN_RDP_WIDTH, le=MAX_RDP_DIMENSION)]
RdpHeight = Annotated[int, Field(strict=True, ge=MIN_RDP_HEIGHT, le=MAX_RDP_DIMENSION)]


def normalize_host(host: str) -> str:
    """Accept only host names/IP literals, never URL authority or query syntax."""
    message = "host must be a DNS/internal hostname, IPv4, or unscoped IPv6 address without URL syntax"
    if (
        not isinstance(host, str)
        or not host
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host)
        or any(char in host for char in "@/\\?#%")
    ):
        raise ValueError(message)
    bracketed = host.startswith("[") and host.endswith("]")
    value = host[1:-1] if bracketed else host
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if bracketed or ":" in value:
            raise ValueError(message) from None
    else:
        if bracketed and address.version != 6:
            raise ValueError(message)
        return str(address)
    try:
        if value.isascii():
            # ASCII resolver names need syntax checks, not Unicode conversion.
            # Preserve private names (including underscores) without IDNA rules.
            ascii_host = value.lower()
        else:
            ascii_host = idna.encode(value, uts46=True, transitional=False, std3_rules=True).decode("ascii").lower()
    except idna.IDNAError:
        raise ValueError(message) from None
    dns_name = ascii_host.removesuffix(".")
    labels = dns_name.split(".")
    if (
        len(dns_name) > 253
        or not all(re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label) for label in labels)
        or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels)
    ):
        raise ValueError(message)
    return ascii_host


def _setting[T: str | int](explicit: T | None, name: str, default: T) -> T | str:
    """Explicit values win; missing/empty environment settings use the default."""
    return explicit if explicit is not None else (os.getenv(name) or default)


def _integer(name: str, value: str | int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be an integer")  # noqa: TRY004 - public configuration-error contract
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _boolean(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


@dataclass(frozen=True)
class RdpConfig:
    host: str
    port: Port
    username: str
    password: str = ""
    domain: str = ""
    width: RdpWidth = DEFAULT_RDP_WIDTH
    height: RdpHeight = DEFAULT_RDP_HEIGHT
    insecure_tls: bool = False
    connect_timeout: float = 30.0
    first_frame_timeout: float = 30.0
    kind: Literal["rdp"] = "rdp"
    secret_env_names: tuple[str, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return (
            f"RdpConfig(host={self.host!r}, port={self.port!r}, username={self.username!r}, "
            f"domain={self.domain!r}, width={self.width!r}, height={self.height!r}, "
            f"insecure_tls={self.insecure_tls!r})"
        )


@dataclass(frozen=True)
class VncConfig:
    host: str
    port: Port
    password: str = ""
    shared: bool = True
    connect_timeout: float = 30.0
    kind: Literal["vnc"] = "vnc"
    secret_env_names: tuple[str, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return f"VncConfig(host={self.host!r}, port={self.port!r}, shared={self.shared!r})"


type BackendConfig = RdpConfig | VncConfig


def load_backend_config(
    backend: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    domain: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> BackendConfig:
    selected = _setting(backend, "REMOTE_COMPUTER_BACKEND", "rdp").strip().lower()
    if selected == "rdp":
        selected_host = _setting(host, "REMOTE_COMPUTER_RDP_HOST", "").strip()
        selected_username = _setting(username, "REMOTE_COMPUTER_RDP_USERNAME", "").strip()
        if not selected_host:
            raise ValueError("RDP host is required (REMOTE_COMPUTER_RDP_HOST)")
        if not selected_username:
            raise ValueError("RDP username is required (REMOTE_COMPUTER_RDP_USERNAME)")
        return RdpConfig(
            host=normalize_host(selected_host),
            port=_integer(
                "RDP port", _setting(port, "REMOTE_COMPUTER_RDP_PORT", DEFAULT_RDP_PORT),
                minimum=MIN_PORT, maximum=MAX_PORT,
            ),
            username=selected_username,
            password=os.getenv("REMOTE_COMPUTER_RDP_PASSWORD", ""),
            domain=_setting(domain, "REMOTE_COMPUTER_RDP_DOMAIN", "").strip(),
            width=_integer(
                "RDP width", _setting(width, "REMOTE_COMPUTER_RDP_WIDTH", DEFAULT_RDP_WIDTH),
                minimum=MIN_RDP_WIDTH, maximum=MAX_RDP_DIMENSION,
            ),
            height=_integer(
                "RDP height", _setting(height, "REMOTE_COMPUTER_RDP_HEIGHT", DEFAULT_RDP_HEIGHT),
                minimum=MIN_RDP_HEIGHT, maximum=MAX_RDP_DIMENSION,
            ),
            insecure_tls=_boolean(os.getenv("REMOTE_COMPUTER_RDP_INSECURE_TLS")),
        )
    if selected == "vnc":
        selected_host = _setting(host, "REMOTE_COMPUTER_VNC_HOST", "").strip()
        if not selected_host:
            raise ValueError("VNC host is required (REMOTE_COMPUTER_VNC_HOST)")
        return VncConfig(
            host=normalize_host(selected_host),
            port=_integer(
                "VNC port", _setting(port, "REMOTE_COMPUTER_VNC_PORT", DEFAULT_VNC_PORT),
                minimum=MIN_PORT, maximum=MAX_PORT,
            ),
            password=os.getenv("REMOTE_COMPUTER_VNC_PASSWORD", ""),
            shared=_boolean(os.getenv("REMOTE_COMPUTER_VNC_SHARED"), default=True),
        )
    raise ValueError("backend must be 'rdp' or 'vnc'")
