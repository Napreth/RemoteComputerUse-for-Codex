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


def save(path: Path, *items: dict) -> None:
    path.write_text(json.dumps({"version": 1, "profiles": items}), encoding="utf-8")


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
