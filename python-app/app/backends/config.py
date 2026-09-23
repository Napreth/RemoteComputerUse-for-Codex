"""Separate configuration models for the RDP and VNC backends."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

BackendKind = Literal["rdp", "vnc"]


def _integer(name: str, value: str | int | None, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else default
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
    port: int
    username: str
    password: str = ""
    domain: str = ""
    width: int = 1440
    height: int = 900
    insecure_tls: bool = False
    connect_timeout: float = 30.0
    first_frame_timeout: float = 30.0
    kind: Literal["rdp"] = "rdp"

    def __repr__(self) -> str:
        return (
            f"RdpConfig(host={self.host!r}, port={self.port!r}, username={self.username!r}, "
            f"domain={self.domain!r}, width={self.width!r}, height={self.height!r}, "
            f"insecure_tls={self.insecure_tls!r})"
        )


@dataclass(frozen=True)
class VncConfig:
    host: str
    port: int
    password: str = ""
    shared: bool = True
    connect_timeout: float = 30.0
    kind: Literal["vnc"] = "vnc"

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
    selected = (backend or os.getenv("REMOTE_COMPUTER_BACKEND") or "rdp").strip().lower()
    if selected == "rdp":
        selected_host = (host or os.getenv("REMOTE_COMPUTER_RDP_HOST") or "").strip()
        selected_username = (username or os.getenv("REMOTE_COMPUTER_RDP_USERNAME") or "").strip()
        if not selected_host:
            raise ValueError("RDP host is required (REMOTE_COMPUTER_RDP_HOST)")
        if not selected_username:
            raise ValueError("RDP username is required (REMOTE_COMPUTER_RDP_USERNAME)")
        return RdpConfig(
            host=selected_host,
            port=_integer("RDP port", port or os.getenv("REMOTE_COMPUTER_RDP_PORT"), 3389, minimum=1, maximum=65535),
            username=selected_username,
            password=os.getenv("REMOTE_COMPUTER_RDP_PASSWORD", ""),
            domain=(domain or os.getenv("REMOTE_COMPUTER_RDP_DOMAIN") or "").strip(),
            width=_integer("RDP width", width or os.getenv("REMOTE_COMPUTER_RDP_WIDTH"), 1440, minimum=320, maximum=8192),
            height=_integer("RDP height", height or os.getenv("REMOTE_COMPUTER_RDP_HEIGHT"), 900, minimum=200, maximum=8192),
            insecure_tls=_boolean(os.getenv("REMOTE_COMPUTER_RDP_INSECURE_TLS")),
        )
    if selected == "vnc":
        selected_host = (host or os.getenv("REMOTE_COMPUTER_VNC_HOST") or "").strip()
        if not selected_host:
            raise ValueError("VNC host is required (REMOTE_COMPUTER_VNC_HOST)")
        return VncConfig(
            host=selected_host,
            port=_integer("VNC port", port or os.getenv("REMOTE_COMPUTER_VNC_PORT"), 5900, minimum=1, maximum=65535),
            password=os.getenv("REMOTE_COMPUTER_VNC_PASSWORD", ""),
            shared=_boolean(os.getenv("REMOTE_COMPUTER_VNC_SHARED"), default=True),
        )
    raise ValueError("backend must be 'rdp' or 'vnc'")
