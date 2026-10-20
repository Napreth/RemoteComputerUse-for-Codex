from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import mcp_server, profiles
from app.backends.config import RdpConfig, VncConfig
from app.profiles import ProfilesError, list_profiles, load_profile


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setattr(profiles, "profiles_path", lambda: path)
    return path


def save(path: Path, *items: dict, **settings) -> None:
    path.write_text(json.dumps({"version": 1, "profiles": items, **settings}), encoding="utf-8")


def rdp(**values) -> dict:
    return {"name": "vm-dev", "type": "rdp", "host": "192.0.2.10", "username": "alice", **values}


def test_profiles_defaults_and_listing_hide_credentials(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", "unrelated-environment-password")
    save(config_file, rdp(password="profile-secret"), {"name": "test", "type": "vnc", "host": "192.0.2.11"})
    before = dict(os.environ)
    assert list_profiles() == [{"name": "vm-dev", "type": "rdp"}, {"name": "test", "type": "vnc"}]
    config = load_profile("vm-dev")
    assert isinstance(config, RdpConfig)
    assert (config.port, config.width, config.height, config.password) == (3389, 1440, 900, "profile-secret")
    assert "profile-secret" not in repr(config)
    vnc = load_profile("test")
    assert isinstance(vnc, VncConfig)
    assert (vnc.port, vnc.shared, vnc.password) == (5900, True, "")
    assert dict(os.environ) == before


def test_profiles_reload_without_changing_existing_snapshot(config_file: Path) -> None:
    save(config_file, rdp(password="old-secret"))
    old = load_profile("vm-dev")
    save(config_file, rdp(host="192.0.2.20", password="new-secret", port=3390, insecure_tls=True))
    new = load_profile("vm-dev")
    assert (old.host, old.password, old.port) == ("192.0.2.10", "old-secret", 3389)
    assert (new.host, new.password, new.port, new.insecure_tls) == ("192.0.2.20", "new-secret", 3390, True)


def test_passwdenv_is_resolved_only_for_selected_profile(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save(config_file, rdp(passwdenv="VM_PASSWORD"), rdp(name="other", passwdenv="OTHER_PASSWORD"))
    monkeypatch.delenv("VM_PASSWORD", raising=False)
    assert len(list_profiles()) == 2
    with pytest.raises(ProfilesError, match="passwdenv.*missing"):
        load_profile("vm-dev")
    monkeypatch.setenv("VM_PASSWORD", "test-env-secret")
    config = load_profile("vm-dev")
    monkeypatch.setenv("VM_PASSWORD", "changed-secret")
    assert config.password == "test-env-secret"
    assert set(config.secret_env_names) == {"VM_PASSWORD", "OTHER_PASSWORD"}
    assert load_profile("vm-dev").password == "changed-secret"
    assert "test-env-secret" not in repr(config)


@pytest.mark.parametrize(
    "contents",
    [
        '{"password": "secret-needle",',
        '{"version":1,"version":1,"profiles":[]}',
        '{"version":1,"profiles":[{"password":"secret-needle","password":"duplicate"}]}',
        json.dumps({"version": True, "profiles": []}),
        json.dumps({"version": 1.0, "profiles": []}),
        json.dumps({"version": 2, "profiles": []}),
        json.dumps({"version": 1, "profiles": [rdp(), rdp()]}),
        json.dumps({"version": 1, "profiles": [rdp(password="secret-needle", passwdenv="VM_PASSWORD")]}),
        json.dumps({"version": 1, "profiles": [rdp(password=123)]}),
        json.dumps({"version": 1, "profiles": [rdp(passwdenv=None)]}),
        json.dumps({"version": 1, "profiles": [rdp(passwdenv="bad env name")]}),
        json.dumps({"version": 1, "profiles": [rdp(port=True)]}),
        json.dumps({"version": 1, "profiles": [rdp(port="3389")]}),
        json.dumps({"version": 1, "profiles": [rdp(port=65536)]}),
        json.dumps({"version": 1, "profiles": [rdp(username=" ")]}),
        json.dumps({"version": 1, "profiles": [rdp(name="bad\nname")]}),
        json.dumps({"version": 1, "profiles": [rdp(unknown="secret-needle")]}),
        json.dumps({"version": 1, "profiles": [rdp(type="secret-needle")]}),
    ],
)
def test_invalid_configuration_never_echoes_contents(config_file: Path, contents: str) -> None:
    config_file.write_text(contents, encoding="utf-8")
    with pytest.raises(ProfilesError) as caught:
        list_profiles()
    assert "secret-needle" not in str(caught.value)
    assert "secret-needle" not in repr(caught.value)


def test_missing_unreadable_oversize_and_unknown_profile(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert list_profiles() == []
    assert not config_file.exists()
    with pytest.raises(ProfilesError, match="missing"):
        load_profile("vm-dev")
    save(config_file, rdp())
    with pytest.raises(ProfilesError, match="not found"):
        load_profile("unknown")
    config_file.write_bytes(b"\xffsecret-needle")
    with pytest.raises(ProfilesError, match="Invalid"):
        list_profiles()
    config_file.write_bytes(b"x" * (profiles.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ProfilesError, match="size limit"):
        list_profiles()
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("secret-needle")))
    with pytest.raises(ProfilesError, match="permissions") as caught:
        list_profiles()
    assert "secret-needle" not in str(caught.value)


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("faß.de", "xn--fa-hia.de"), ("straße.de", "xn--strae-oqa.de"),
        ("ς.gr", "xn--3xa.gr"), ("σ.gr", "xn--4xa.gr"),
        ("rdp_service", "rdp_service"), ("vnc_server", "vnc_server"),
        ("WIN_Host.Local.", "win_host.local."),
        ("ab--host.local", "ab--host.local"), ("xn--fa-hia.de", "xn--fa-hia.de"),
    ],
)
def test_profile_hosts_preserve_idna2008_and_internal_names(config_file, backend, host, expected):
    profile = {"name": "test", "type": backend, "host": host, "password": "profile-secret"}
    if backend == "rdp":
        profile["username"] = "alice"
    save(config_file, profile)
    assert list_profiles() == [{"name": "test", "type": backend}]
    config = load_profile("test")
    assert (config.host, config.password) == (expected, "profile-secret")


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
@pytest.mark.parametrize("host", ["127.0.0.1@evil.example", "host/path", "host?serverip=evil", "host#x", "host%40evil"])
def test_profiles_reject_url_hosts_without_resolving_secrets(config_file, monkeypatch, backend, host):
    profile = {"name": "bad", "type": backend, "host": host, "passwdenv": "MISSING_SECRET"}
    if backend == "rdp":
        profile["username"] = "alice"
    save(config_file, profile)
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    for operation in (list_profiles, lambda: load_profile("bad")):
        with pytest.raises(ProfilesError, match="Invalid Profiles"):
            operation()


@pytest.mark.parametrize("shared", [False, 0, 1, "true"])
def test_vnc_profiles_reject_unsupported_or_nonboolean_shared_values(config_file, shared):
    save(config_file, {"name": "vnc", "type": "vnc", "host": "127.0.0.1", "shared": shared})
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        list_profiles()
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        load_profile("vnc")


async def test_missing_profiles_do_not_block_environment_connection(config_file, monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTER_BACKEND", "rdp")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "rdp.example")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "alice")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", "legacy-secret")
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    before = dict(os.environ)
    assert await mcp_server.computer_list_profiles() == {"profiles": []}
    assert await mcp_server.computer_connect(name="legacy") == {"connected": True}
    config, name = connect.await_args.args
    assert (config.host, config.password, name) == ("rdp.example", "legacy-secret", "legacy")
    assert dict(os.environ) == before
    assert not config_file.exists()
    with pytest.raises(Exception, match="missing"):
        await mcp_server.computer_connect(profile="explicit", name="another")
    connect.assert_awaited_once()


async def test_invalid_profile_host_never_launches_runtime(config_file, monkeypatch):
    save(config_file, rdp(host="127.0.0.1@evil.example", password="secret-needle"))
    connect = AsyncMock()
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    with pytest.raises(Exception, match="Invalid Profiles") as caught:
        await mcp_server.computer_connect(profile="vm-dev", name="work")
    assert "secret-needle" not in str(caught.value)
    connect.assert_not_awaited()


def test_windows_profile_path_and_native_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    expected = tmp_path / "remote-computer-use" / "config.json"
    assert profiles.profiles_path() == expected
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(profiles, "_windows_local_app_data", lambda: tmp_path)
    assert profiles.profiles_path() == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows Known Folder API")
def test_windows_native_lookup_without_forwarded_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = profiles._windows_local_app_data()
    assert root.is_absolute()
    assert profiles.profiles_path() == root / "remote-computer-use" / "config.json"


def test_linux_profile_path_uses_explicit_xdg_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert profiles.profiles_path() == tmp_path / "remote-computer-use" / "config.json"


@pytest.mark.parametrize("xdg_root", [None, "", "relative-path"])
def test_linux_without_absolute_xdg_root_uses_private_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg_root: str | None,
) -> None:
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if xdg_root is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg_root)
    assert profiles.profiles_path() == tmp_path / ".remote-computer-use" / "config.json"
    assert not list(tmp_path.iterdir())


async def test_mcp_profile_selection_is_separate_from_session_name(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    save(config_file, rdp(password="profile-secret"))
    connect = AsyncMock(return_value={"connected": True, "name": "work"})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    assert await mcp_server.computer_list_profiles() == {"profiles": [{"name": "vm-dev", "type": "rdp"}]}
    await mcp_server.computer_connect(profile="vm-dev", name="work")
    config, name = connect.await_args.args
    assert (config.host, config.password, name) == ("192.0.2.10", "profile-secret", "work")
    for override in ({"host": "192.0.2.20"}, {"backend": "vnc"}, {"port": 3390}, {"username": "bob"}):
        with pytest.raises(Exception, match="cannot be combined"):
            await mcp_server.computer_connect(profile="vm-dev", **override)
    assert connect.await_count == 1


@pytest.mark.parametrize("env_name", ["PROFILE_PASSWORD", "profile_password"])
async def test_profile_env_is_removed_before_worker_spawn(env_name, config_file, monkeypatch):
    from app.remote.runtime import launch_remote_python_runtime

    monkeypatch.setenv(env_name, "worker-must-not-see-this")
    monkeypatch.setenv("OTHER_PASSWORD", "other-profile-secret")
    save(config_file, rdp(passwdenv=env_name), rdp(name="other", passwdenv="OTHER_PASSWORD"))
    config = load_profile("vm-dev")
    helper = config_file.parent / "release_snapshot.py"
    marker = config_file.parent / "release_snapshot.ok"
    helper.write_text(
        "import json, os\n"
        f"assert os.getenv({env_name!r}) is None\n"
        "assert os.getenv('OTHER_PASSWORD') is None\n"
        "assert os.getenv('REMOTE_COMPUTER_RDP_PASSWORD') == 'worker-must-not-see-this'\n"
        f"open({str(marker)!r}, 'w').write('released')\n"
        "print(json.dumps({'released': True}))\n",
        encoding="utf-8",
    )
    session = await launch_remote_python_runtime(
        config,
        worker_path=Path(__file__).parent / "fixtures" / "worker.py",
        release_helper_path=helper,
    )
    try:
        result = await session.execute(f"print(os.getenv({env_name!r}), os.getenv('OTHER_PASSWORD'))")
        assert result["output"][0]["text"] == "None None\n"
        assert session.environment["REMOTE_COMPUTER_RDP_PASSWORD"] == "worker-must-not-see-this"
        assert os.environ[env_name] == "worker-must-not-see-this"
        monkeypatch.setenv(env_name, "new-env-secret")
        save(config_file, rdp(password="new-file-secret"))
        await session.execute("input_held = True")
    finally:
        await session.close()
    assert marker.read_text() == "released"


def test_agent_features_default_off_even_when_called_directly(config_file):
    assert profiles.agent_features() == (False, False)
    with pytest.raises(ProfilesError, match="disabled"):
        profiles.load_filled_profile("vm-dev", {"password": "secret-needle"})
    with pytest.raises(ProfilesError, match="disabled"):
        profiles.load_agent_profile({"type": "vnc", "host": "localhost", "password": "secret-needle"})
    assert not config_file.exists()


@pytest.mark.parametrize("switch", ["enableDeclareAsFillByAgent", "enableProfileFromAgent"])
@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_agent_feature_switches_require_real_booleans(config_file, switch, value):
    save(config_file, **{switch: value})
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        profiles.agent_features()


@pytest.mark.parametrize("declared", [
    ["name"], ["type"], ["passwdenv"], ["fillByAgent"], ["unknown"], ["host", "host"], [1], "host",
])
def test_invalid_agent_field_declarations_are_rejected(config_file, declared):
    save(config_file, rdp(fillByAgent=declared), enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        list_profiles()


def test_agent_fill_template_and_snapshot_do_not_persist_values(config_file, monkeypatch):
    template = {"name": "vm-dev", "type": "rdp", "username": "alice", "fillByAgent": ["host", "password"]}
    save(config_file, template, enableDeclareAsFillByAgent=True)
    original = config_file.read_bytes()
    environment = dict(os.environ)
    assert list_profiles() == [{"name": "vm-dev", "type": "rdp", "fillByAgent": ["host", "password"]}]
    with pytest.raises(ProfilesError, match="computer_connect_filled_profile"):
        load_profile("vm-dev")
    config = profiles.load_filled_profile("vm-dev", {"host": "faß.de", "password": "first-secret"})
    assert (config.host, config.username, config.password) == ("xn--fa-hia.de", "alice", "first-secret")
    assert "first-secret" not in repr(config)
    assert config_file.read_bytes() == original
    assert dict(os.environ) == environment
    assert "first-secret" not in str(list_profiles())
    other = profiles.load_filled_profile("vm-dev", {"host": "192.0.2.20", "password": "second-secret"})
    assert (config.host, config.password) == ("xn--fa-hia.de", "first-secret")
    assert (other.host, other.password) == ("192.0.2.20", "second-secret")
    save(config_file, template, enableDeclareAsFillByAgent=False)
    assert list_profiles() == [{"name": "vm-dev", "type": "rdp"}]
    with pytest.raises(ProfilesError, match="disabled"):
        profiles.load_filled_profile("vm-dev", {"host": "localhost", "password": "secret-needle"})


@pytest.mark.parametrize("fields", [
    {}, {"host": "localhost"}, {"host": "localhost", "password": "secret-needle", "name": "other"},
    {"host": "localhost", "password": "secret-needle", "passwdenv": "OTHER_SECRET"},
    {"host": "localhost", "password": "secret-needle", "username": "other"},
    {"host": "localhost", "password": "secret-needle", "fillByAgent": ["username"]},
])
def test_agent_fill_cannot_change_undeclared_fields_or_omit_declared_fields(config_file, fields):
    template = rdp(fillByAgent=["host", "password"])
    del template["host"]
    save(config_file, template, enableDeclareAsFillByAgent=True)
    before = config_file.read_bytes()
    with pytest.raises(ProfilesError, match="exactly") as caught:
        profiles.load_filled_profile("vm-dev", fields)
    assert "secret-needle" not in str(caught.value)
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("host", [None, "", "127.0.0.1@evil.example", "host?serverip=evil"])
def test_agent_fill_still_validates_host_without_echoing_credentials(config_file, host):
    template = rdp(fillByAgent=["host", "password"])
    del template["host"]
    save(config_file, template, enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="Invalid Agent") as caught:
        profiles.load_filled_profile("vm-dev", {"host": host, "password": "secret-needle"})
    assert "secret-needle" not in str(caught.value)


def test_agent_fill_declarations_must_match_backend_and_password_source(config_file):
    for item in (
        {"name": "vnc", "type": "vnc", "host": "localhost", "fillByAgent": ["username"]},
        rdp(passwdenv="SAVED_SECRET", fillByAgent=["password"]),
    ):
        save(config_file, item, enableDeclareAsFillByAgent=True)
        with pytest.raises(ProfilesError, match="Invalid Profiles"):
            list_profiles()


def test_agent_filled_username_and_vnc_defaults(config_file):
    save(config_file, {"name": "rdp", "type": "rdp", "host": "localhost", "fillByAgent": ["username"]},
         {"name": "vnc", "type": "vnc", "fillByAgent": ["host", "password"]},
         enableDeclareAsFillByAgent=True)
    assert profiles.load_filled_profile("rdp", {"username": "alice"}).username == "alice"
    config = profiles.load_filled_profile("vnc", {"host": "::1", "password": "secret-needle"})
    assert isinstance(config, VncConfig)
    assert (config.host, config.port, config.shared) == ("::1", 5900, True)


@pytest.mark.parametrize("values", [
    {"host": None}, {"username": None}, {"host": ""}, {"username": ""},
])
def test_explicit_null_or_empty_template_fields_remain_invalid(config_file, values):
    save(config_file, rdp(**values, fillByAgent=list(values)), enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        list_profiles()


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
def test_direct_agent_settings_use_no_environment_defaults_or_persistent_changes(config_file, monkeypatch, backend):
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", "unrelated-rdp-secret")
    monkeypatch.setenv("REMOTE_COMPUTER_VNC_PASSWORD", "unrelated-vnc-secret")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PORT", "3399")
    monkeypatch.setenv("UV_CACHE_DIR", "unchanged-cache")
    monkeypatch.setenv("CUSTOM_SECRET", "saved-profile-secret")
    save(config_file, rdp(passwdenv="CUSTOM_SECRET"), enableProfileFromAgent=True)
    original = config_file.read_bytes()
    environment = dict(os.environ)
    data = {"type": backend, "host": "[::1]"}
    if backend == "rdp":
        data["username"] = "alice"
    config = profiles.load_agent_profile(data)
    assert config.password == ""
    assert config.host == "::1"
    assert config.port == (3389 if backend == "rdp" else 5900)
    assert config.secret_env_names == ("CUSTOM_SECRET",)
    assert config_file.read_bytes() == original
    assert dict(os.environ) == environment
    save(config_file)
    with pytest.raises(ProfilesError, match="disabled"):
        profiles.load_agent_profile(data)


@pytest.mark.parametrize("extra", [
    {"name": "other"}, {"passwdenv": "OTHER_SECRET"}, {"fillByAgent": ["host"]},
    {"host": "127.0.0.1@evil.example"}, {"host": None}, {"port": 0}, {"port": True},
    {"unknown": "secret-needle"}, {"shared": False},
])
def test_direct_agent_settings_reject_invalid_or_profile_only_fields(config_file, extra):
    save(config_file, enableProfileFromAgent=True)
    with pytest.raises(ProfilesError) as caught:
        profiles.load_agent_profile({"type": "vnc", "host": "localhost", "password": "secret-needle", **extra})
    assert "secret-needle" not in str(caught.value)


async def test_agent_tools_only_register_when_enabled_and_recheck_revocation(config_file, monkeypatch):
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    for fill, direct in ((False, False), (True, False), (False, True), (True, True)):
        save(config_file, rdp(fillByAgent=["password"]),
             enableDeclareAsFillByAgent=fill, enableProfileFromAgent=direct)
        server = mcp_server.RemoteComputerMCPServer("test-agent-features")
        mcp_server.register_agent_tools(server)
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert ("computer_connect_filled_profile" in tools) is fill
        assert ("computer_connect_from_agent" in tools) is direct
        if direct:
            schema = tools["computer_connect_from_agent"].input_schema
            assert "password" in schema["properties"]
            assert "passwdenv" not in schema["properties"]
            await server.call_tool("computer_connect_from_agent", {
                "backend": "rdp", "host": "localhost", "username": "alice", "password": "direct-secret", "name": "direct",
            })
            config, name = connect.await_args.args
            assert (config.host, config.password, name) == ("localhost", "direct-secret", "direct")
        if fill:
            await server.call_tool("computer_connect_filled_profile", {
                "profile": "vm-dev", "fields": {"password": "filled-secret"}, "name": "filled",
            })
            config, name = connect.await_args.args
            assert (config.host, config.password, name) == ("192.0.2.10", "filled-secret", "filled")
        calls = connect.await_count
        save(config_file, rdp(fillByAgent=["password"]))
        if direct:
            with pytest.raises(Exception, match="disabled"):
                await server.call_tool("computer_connect_from_agent", {"backend": "vnc", "host": "localhost"})
        if fill:
            with pytest.raises(Exception, match="disabled"):
                await server.call_tool("computer_connect_filled_profile", {
                    "profile": "vm-dev", "fields": {"password": "secret-needle"},
                })
        assert connect.await_count == calls


@pytest.mark.parametrize("mode", ["filled", "direct", "saved", "environment"])
async def test_connection_backend_errors_redact_credentials(config_file, monkeypatch, mode):
    from urllib.parse import quote

    password = "secret@needle/测试"
    encoded = quote(password, safe="")
    save(config_file, rdp(fillByAgent=["password"]),
         enableDeclareAsFillByAgent=True, enableProfileFromAgent=True)
    if mode == "saved":
        save(config_file, rdp(password=password))
    elif mode == "environment":
        monkeypatch.setenv("REMOTE_COMPUTER_BACKEND", "rdp")
        monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "localhost")
        monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "alice")
        monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", password)
    monkeypatch.setattr(mcp_server.sessions, "connect", AsyncMock(side_effect=RuntimeError(f"failed {password} / {encoded}")))
    with pytest.raises(Exception, match="redacted") as caught:
        if mode == "filled":
            await mcp_server.computer_connect_filled_profile("vm-dev", {"password": password})
        elif mode == "direct":
            await mcp_server.computer_connect_from_agent("rdp", "localhost", username="alice", password=password)
        else:
            await mcp_server.computer_connect(profile="vm-dev" if mode == "saved" else None)
    assert password not in str(caught.value)
    assert encoded not in str(caught.value)


async def test_invalid_configuration_does_not_register_agent_tools(config_file):
    config_file.write_text('{"enableProfileFromAgent": true, "password": "secret-needle",', encoding="utf-8")
    server = mcp_server.RemoteComputerMCPServer("invalid-agent-config")
    mcp_server.register_agent_tools(server)
    assert await server.list_tools() == []


@pytest.mark.parametrize("value", [0, True, 65536, "3389"])
def test_filled_numeric_fields_keep_strict_validation(config_file, value):
    save(config_file, rdp(fillByAgent=["port"]), enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="Invalid Agent"):
        profiles.load_filled_profile("vm-dev", {"port": value})


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(("backend", "field", "value"), [
    ("rdp", "host", "localhost"), ("rdp", "username", "alice"), ("rdp", "password", ""),
    ("rdp", "port", 3389), ("rdp", "domain", ""), ("rdp", "width", 1440), ("rdp", "height", 900),
    ("rdp", "insecure_tls", False), ("vnc", "shared", True),
])
def test_fill_by_agent_fields_cannot_also_be_present_in_saved_profile(config_file, enabled, backend, field, value):
    item = {"name": "template", "type": backend, "host": "localhost", "fillByAgent": [field], field: value}
    if backend == "rdp":
        item["username"] = "alice"
    save(config_file, item, enableDeclareAsFillByAgent=enabled)
    original = config_file.read_bytes()
    with pytest.raises(ProfilesError, match="fillByAgent fields are absent"):
        list_profiles()
    assert config_file.read_bytes() == original


def test_agent_filling_works_when_declared_fields_have_only_implicit_defaults(config_file):
    save(config_file, {"name": "vnc", "type": "vnc", "fillByAgent": ["host", "password", "port", "shared"]},
         enableDeclareAsFillByAgent=True)
    config = profiles.load_filled_profile("vnc", {
        "host": "localhost", "password": "secret-needle", "port": 5999, "shared": True,
    })
    assert (config.host, config.password, config.port, config.shared) == ("localhost", "secret-needle", 5999, True)


@pytest.mark.parametrize("declared", [[], ["type"], ["host"]])
def test_missing_profile_type_is_rejected(config_file, declared):
    save(config_file, {"name": "template", "host": "localhost", "fillByAgent": declared},
         enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="Invalid Profiles"):
        list_profiles()


def test_agent_cannot_change_a_saved_profile_backend(config_file):
    save(config_file, rdp(fillByAgent=["password"]), enableDeclareAsFillByAgent=True)
    with pytest.raises(ProfilesError, match="type.*cannot be filled"):
        profiles.load_filled_profile("vm-dev", {"type": "vnc", "password": "secret-needle"})
    assert profiles.load_filled_profile("vm-dev", {"password": "secret-needle"}).kind == "rdp"


@pytest.mark.parametrize("mode", ["direct", "filled"])
@pytest.mark.parametrize("password", [
    "null", "[]", "{}", '["test"]', '{"test": 1}', "true", "false", "123456", '"quoted"', " null ", "中文😀",
])
async def test_mcp_password_strings_are_never_reinterpreted_as_json(config_file, monkeypatch, mode, password):
    save(config_file, rdp(fillByAgent=["password"]),
         enableDeclareAsFillByAgent=True, enableProfileFromAgent=True)
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    server = mcp_server.RemoteComputerMCPServer("literal-passwords")
    mcp_server.register_agent_tools(server)
    if mode == "direct":
        await server.call_tool("computer_connect_from_agent", {
            "backend": "rdp", "host": "localhost", "username": "alice", "password": password,
        })
    else:
        await server.call_tool("computer_connect_filled_profile", {"profile": "vm-dev", "fields": {"password": password}})
    assert connect.await_args.args[0].password == password


@pytest.mark.parametrize("profile", ["null", "[]", "{}", '["test"]'])
async def test_json_shaped_profile_names_never_fall_back_to_environment(config_file, monkeypatch, profile):
    save(config_file, rdp(name=profile, password="profile-secret"))
    for key in tuple(os.environ):
        if key.startswith("REMOTE_COMPUTER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("REMOTE_COMPUTER_BACKEND", "rdp")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "wrong-target.example")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "wrong-user")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", "wrong-environment-secret")
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    await mcp_server.mcp.call_tool("computer_connect", {"profile": profile, "name": "null"})
    config, name = connect.await_args.args
    assert (config.host, config.username, config.password, name) == ("192.0.2.10", "alice", "profile-secret", "null")


async def test_optional_agent_strings_and_session_names_remain_literal(config_file, monkeypatch):
    save(config_file, enableProfileFromAgent=True)
    connect = AsyncMock(return_value={"connected": True})
    status = AsyncMock(return_value={"connected": False})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    monkeypatch.setattr(mcp_server.sessions, "status", status)
    server = mcp_server.RemoteComputerMCPServer("literal-identity")
    mcp_server.register_agent_tools(server)
    await server.call_tool("computer_connect_from_agent", {
        "backend": "rdp", "host": "localhost", "username": "null", "domain": "null", "name": "null",
    })
    config, name = connect.await_args.args
    assert (config.username, config.domain, name) == ("null", "null", "null")
    await mcp_server.mcp.call_tool("computer_status", {"session": "null"})
    status.assert_awaited_once_with("null")


@pytest.mark.parametrize(("tool", "arguments"), [
    ("computer_connect", {"profile": "vm-dev", "password": "secret-needle"}),
    ("computer_connect_from_agent", {"backend": "vnc", "host": "localhost", "passwdenv": "secret-needle"}),
    ("computer_connect_from_agent", {"backend": "vnc", "host": "localhost", "password": "secret-needle", "type": "rdp"}),
    ("computer_connect_filled_profile", {
        "profile": "vm-dev", "fields": {"password": "secret-needle"}, "host": "unexpected.example",
    }),
])
async def test_mcp_connection_tools_reject_unknown_arguments_without_echoing_secrets(
    config_file, monkeypatch, tool, arguments,
):
    from mcp.server.mcpserver.exceptions import ToolError

    save(config_file, rdp(fillByAgent=["password"]),
         enableDeclareAsFillByAgent=True, enableProfileFromAgent=True)
    connect = AsyncMock(return_value={"connected": True})
    monkeypatch.setattr(mcp_server.sessions, "connect", connect)
    server = mcp_server.mcp if tool == "computer_connect" else mcp_server.RemoteComputerMCPServer("unknown-arguments")
    if server is not mcp_server.mcp:
        mcp_server.register_agent_tools(server)
    with pytest.raises(ToolError) as caught:
        await server.call_tool(tool, arguments)
    assert "secret-needle" not in str(caught.value)
    connect.assert_not_awaited()
    for info in await server.list_tools():
        assert info.input_schema["additionalProperties"] is False
