"""Read user-owned remote connection profiles without changing the environment."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

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

CONFIG_DIRECTORY = "remote-computer-use"
MAX_CONFIG_BYTES = 1024 * 1024
type FillField = Literal[
    "host", "port", "username", "password", "domain", "width", "height", "insecure_tls", "shared",
]


class ProfilesError(ValueError):
    """A user configuration error safe to return through MCP."""


def _windows_local_app_data() -> Path:
    if sys.platform != "win32":
        raise ProfilesError("Windows LocalAppData lookup is only available on Windows.")
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
        if not value or not Path(value).is_absolute():
            return Path.home() / f".{CONFIG_DIRECTORY}" / "config.json"
        root = Path(value)
    return root / CONFIG_DIRECTORY / "config.json"


class _Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=1024)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    passwdenv: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", repr=False)
    fillByAgent: list[FillField] = Field(default_factory=list, max_length=9)

    @field_validator("name", "host")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not isinstance(value, str) or value != value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Expected nonblank text without surrounding whitespace or control characters")
        return value

    @field_validator("host")
    @classmethod
    def hostname_or_ip(cls, value: str) -> str:
        return normalize_host(value)

    @model_validator(mode="after")
    def connection_sources(self) -> Self:
        if "passwdenv" in self.model_fields_set and (self.passwdenv is None or "password" in self.model_fields_set):
            raise ValueError("password and passwdenv are mutually exclusive")
        if self.passwdenv is not None and "password" in self.fillByAgent:
            raise ValueError("Agent password filling cannot be combined with passwdenv")
        allowed = type(self).model_fields.keys() - {"name", "type", "passwdenv", "fillByAgent"}
        if len(set(self.fillByAgent)) != len(self.fillByAgent) or set(self.fillByAgent) - allowed:
            raise ValueError("fillByAgent must contain unique connection fields supported by this profile type")
        if self.host is None and "host" not in self.fillByAgent:
            raise ValueError("host is required unless declared in fillByAgent")
        return self


class _RdpProfile(_Profile):
    type: Literal["rdp"]
    port: Port = DEFAULT_RDP_PORT
    username: str | None = Field(default=None, min_length=1, max_length=1024)
    domain: str = ""
    width: RdpWidth = DEFAULT_RDP_WIDTH
    height: RdpHeight = DEFAULT_RDP_HEIGHT
    insecure_tls: bool = False

    @field_validator("username")
    @classmethod
    def nonblank_username(cls, value: str) -> str:
        return cls.nonblank_text(value)

    @model_validator(mode="after")
    def username_source(self) -> Self:
        if self.username is None and "username" not in self.fillByAgent:
            raise ValueError("username is required unless declared in fillByAgent")
        return self


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


type Profile = Annotated[_RdpProfile | _VncProfile, Field(discriminator="type")]
_profile_adapter: TypeAdapter[Profile] = TypeAdapter(Profile)


class _Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    profiles: list[Profile] = Field(max_length=1000)
    enableDeclareAsFillByAgent: bool = False
    enableProfileFromAgent: bool = False

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be integer 1")
        return value

    @model_validator(mode="after")
    def valid_profiles(self) -> Self:
        if len({profile.name for profile in self.profiles}) != len(self.profiles):
            raise ValueError("Profile names must be unique")
        if any(set(profile.fillByAgent) & profile.model_fields_set for profile in self.profiles):
            raise ValueError("Fields declared in fillByAgent must be absent from the saved profile")
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
            "Invalid Profiles config.json; check version 1, field types, unique names, password/passwdenv, "
            "and that fillByAgent fields are absent from the profile."
        ) from None


def agent_features() -> tuple[bool, bool]:
    """Read switches from user configuration, never from connection arguments."""
    document = _read_profiles(missing_ok=True)
    return document.enableDeclareAsFillByAgent, document.enableProfileFromAgent


def list_profiles() -> list[dict[str, Any]]:
    document = _read_profiles(missing_ok=True)
    result: list[dict[str, Any]] = []
    for profile in document.profiles:
        item: dict[str, Any] = {"name": profile.name, "type": profile.type}
        if document.enableDeclareAsFillByAgent and profile.fillByAgent:
            item["fillByAgent"] = profile.fillByAgent.copy()
        result.append(item)
    return result


def _select_profile(document: _Profiles, name: str) -> Profile:
    selected = next((profile for profile in document.profiles if profile.name == name), None)
    if selected is None:
        raise ProfilesError("Requested profile was not found in config.json.")
    return selected


def _validate_agent_profile(values: dict[str, Any]) -> Profile:
    try:
        return _profile_adapter.validate_python(values)
    except (ValueError, ValidationError):
        raise ProfilesError("Invalid Agent connection fields; check the backend, host, field types and credentials.") from None


def load_profile(name: str) -> BackendConfig:
    document = _read_profiles()
    selected = _select_profile(document, name)
    if document.enableDeclareAsFillByAgent and selected.fillByAgent:
        raise ProfilesError("This profile requires Agent fields; use computer_connect_filled_profile.")
    return _backend_config(selected, document)


def load_filled_profile(name: str, fields: dict[str, Any]) -> BackendConfig:
    document = _read_profiles(missing_ok=True)
    if not document.enableDeclareAsFillByAgent:
        raise ProfilesError("Agent profile filling is disabled; enableDeclareAsFillByAgent must be true in config.json.")
    selected = _select_profile(document, name)
    if not selected.fillByAgent or set(fields) != set(selected.fillByAgent):
        raise ProfilesError(
            "Supply exactly the profile's fillByAgent fields; name, type and passwdenv cannot be filled."
        )
    values = selected.model_dump(exclude_unset=True)
    # Declarations describe the saved template, not the completed connection.
    # Remove them before validating all required fields of the profile's backend.
    values.pop("fillByAgent", None)
    values.update(fields)
    return _backend_config(_validate_agent_profile(values), document)


def load_agent_profile(values: dict[str, Any]) -> BackendConfig:
    document = _read_profiles(missing_ok=True)
    if not document.enableProfileFromAgent:
        raise ProfilesError("Agent connections are disabled; enableProfileFromAgent must be true in config.json.")
    if set(values) & {"name", "passwdenv", "fillByAgent"}:
        raise ProfilesError("Agent connections cannot set profile name, passwdenv or fillByAgent.")
    selected = _validate_agent_profile({**values, "name": "agent"})
    return _backend_config(selected, document)


def _backend_config(selected: Profile, document: _Profiles) -> BackendConfig:
    if selected.host is None or (isinstance(selected, _RdpProfile) and selected.username is None):
        raise ProfilesError("The selected profile is missing required connection fields; supply its declared Agent fields.")
    password = selected.password.get_secret_value()
    if selected.passwdenv is not None:
        value = os.environ.get(selected.passwdenv)
        if value is None:
            raise ProfilesError("The selected profile's passwdenv variable is missing from the MCP process environment.")
        password = value
    # Capture the resolved credentials and names once per connection. Cleanup
    # uses this snapshot even if the user edits the file or environment later.
    secret_env_names = tuple(profile.passwdenv for profile in document.profiles if profile.passwdenv is not None)
    if isinstance(selected, _RdpProfile):
        assert selected.username is not None
        return RdpConfig(
            host=selected.host, port=selected.port, username=selected.username, password=password,
            domain=selected.domain, width=selected.width, height=selected.height, insecure_tls=selected.insecure_tls,
            secret_env_names=secret_env_names,
        )
    return VncConfig(
        host=selected.host, port=selected.port, password=password, shared=selected.shared,
        secret_env_names=secret_env_names,
    )
