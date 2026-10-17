"""Read user-owned remote connection profiles without changing the environment."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from .backends.config import (
    DEFAULT_RDP_HEIGHT,
    DEFAULT_RDP_PORT,
    DEFAULT_RDP_WIDTH,
    DEFAULT_VNC_PORT,
    BackendConfig,
    Port,
    RdpConfig,
    RdpHeight,
    RdpWidth,
    VncConfig,
    normalize_host,
)

CONFIG_DIRECTORY = "remote-computer-use-for-codex"
MAX_CONFIG_BYTES = 1024 * 1024


class ProfilesError(ValueError):
    """A user configuration error safe to return through MCP."""


def _windows_local_app_data() -> Path:
    # Codex may omit LOCALAPPDATA from the MCP environment. Query the current
    # user's Windows folder instead of requiring another forwarded variable.
    from ctypes import wintypes

    folder = ctypes.WinDLL("shell32").SHGetFolderPathW
    folder.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    folder.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(260)
    if folder(None, 0x001C, None, 0, buffer) != 0 or not buffer.value:
        raise ProfilesError("Cannot locate the current user's LocalAppData directory.")
    return Path(buffer.value)


def profiles_path() -> Path:
    if sys.platform == "win32":
        value = os.environ.get("LOCALAPPDATA")
        root = Path(value) if value else _windows_local_app_data()
        if not root.is_absolute():
            raise ProfilesError("LOCALAPPDATA must be an absolute directory.")
    else:
        value = os.environ.get("XDG_CONFIG_HOME")
        root = Path(value) if value and Path(value).is_absolute() else Path.home() / ".config"
    return root / CONFIG_DIRECTORY / "config.json"


class _Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=1024)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    passwdenv: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", repr=False)

    @field_validator("name", "host")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Expected nonblank text without surrounding whitespace or control characters")
        return value

    @field_validator("host")
    @classmethod
    def hostname_or_ip(cls, value: str) -> str:
        return normalize_host(value)

    @model_validator(mode="after")
    def password_source(self) -> Self:
        if "passwdenv" in self.model_fields_set and (self.passwdenv is None or "password" in self.model_fields_set):
            raise ValueError("password and passwdenv are mutually exclusive")
        return self


class _RdpProfile(_Profile):
    type: Literal["rdp"]
    port: Port = DEFAULT_RDP_PORT
    username: str = Field(min_length=1, max_length=1024)
    domain: str = ""
    width: RdpWidth = DEFAULT_RDP_WIDTH
    height: RdpHeight = DEFAULT_RDP_HEIGHT
    insecure_tls: bool = False

    @field_validator("username")
    @classmethod
    def nonblank_username(cls, value: str) -> str:
        return cls.nonblank_text(value)


class _VncProfile(_Profile):
    type: Literal["vnc"]
    port: Port = DEFAULT_VNC_PORT
    shared: Literal[True] = True

    @field_validator("shared", mode="before")
    @classmethod
    def shared_sessions_only(cls, value: object) -> object:
        if value is not True:
            raise ValueError("VNC profiles support shared=true only")
        return value


class _Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    profiles: list[Annotated[_RdpProfile | _VncProfile, Field(discriminator="type")]] = Field(max_length=1000)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be integer 1")
        return value

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        if len({profile.name for profile in self.profiles}) != len(self.profiles):
            raise ValueError("Profile names must be unique")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _read_profiles(*, missing_ok: bool = False) -> _Profiles:
    try:
        with profiles_path().open("rb") as stream:
            contents = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        if missing_ok:
            return _Profiles(version=1, profiles=[])
        raise ProfilesError("Profiles config.json is missing; create it in the documented user configuration directory.") from None
    except OSError:
        raise ProfilesError("Cannot read Profiles config.json; check its location and file permissions.") from None
    if len(contents) > MAX_CONFIG_BYTES:
        raise ProfilesError("Profiles config.json exceeds the 1 MiB size limit.")
    try:
        return _Profiles.model_validate(json.loads(contents.decode("utf-8-sig"), object_pairs_hook=_unique_object))
    except (ValueError, ValidationError, RecursionError):
        # Never echo JSON, validation inputs, or parser excerpts: they may contain
        # plaintext passwords, including in malformed fields.
        raise ProfilesError(
            "Invalid Profiles config.json; check version 1, field types, unique names, and password/passwdenv."
        ) from None


def list_profiles() -> list[dict[str, str]]:
    return [{"name": profile.name, "type": profile.type} for profile in _read_profiles(missing_ok=True).profiles]


def load_profile(name: str) -> BackendConfig:
    profiles = _read_profiles().profiles
    selected = next((profile for profile in profiles if profile.name == name), None)
    if selected is None:
        raise ProfilesError("Requested profile was not found in config.json.")
    password = selected.password.get_secret_value()
    if selected.passwdenv is not None:
        value = os.environ.get(selected.passwdenv)
        if value is None:
            raise ProfilesError("The selected profile's passwdenv variable is missing from the MCP process environment.")
        password = value
    # Capture the resolved credentials and names once per connection. Cleanup
    # uses this snapshot even if the user edits the file or environment later.
    secret_env_names = tuple(profile.passwdenv for profile in profiles if profile.passwdenv is not None)
    if isinstance(selected, _RdpProfile):
        return RdpConfig(
            host=selected.host, port=selected.port, username=selected.username, password=password,
            domain=selected.domain, width=selected.width, height=selected.height, insecure_tls=selected.insecure_tls,
            secret_env_names=secret_env_names,
        )
    return VncConfig(
        host=selected.host, port=selected.port, password=password, shared=selected.shared,
        secret_env_names=secret_env_names,
    )
