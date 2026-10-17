import os

import pytest

from app.backends.config import RdpConfig, VncConfig, load_backend_config, normalize_host


@pytest.fixture
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "BACKEND": "rdp", "RDP_HOST": "env-rdp.example", "RDP_USERNAME": "env-user",
        "RDP_DOMAIN": "ENV-DOMAIN", "RDP_PORT": "3391", "RDP_WIDTH": "1600", "RDP_HEIGHT": "1000",
        "VNC_HOST": "env-vnc.example", "VNC_PORT": "5901",
    }.items():
        monkeypatch.setenv(f"REMOTE_COMPUTER_{name}", value)


@pytest.mark.parametrize(("backend", "field"), [("rdp", "port"), ("rdp", "width"), ("rdp", "height"), ("vnc", "port")])
@pytest.mark.parametrize("value", [0, True, False, 1.5, 4000.0])
def test_invalid_explicit_numbers_never_fall_back_to_environment(configured_environment, backend, field, value):
    with pytest.raises(ValueError, match=field):
        load_backend_config(backend, **{field: value})


@pytest.mark.parametrize(
    ("backend", "field", "value"),
    [("rdp", "port", 65536), ("vnc", "port", 65536), ("rdp", "width", 319), ("rdp", "height", 199),
     ("rdp", "width", 8193), ("rdp", "height", 8193)],
)
def test_explicit_numbers_still_enforce_backend_ranges(configured_environment, backend, field, value):
    with pytest.raises(ValueError, match=field):
        load_backend_config(backend, **{field: value})


@pytest.mark.parametrize(("backend", "field"), [("rdp", "host"), ("rdp", "username"), ("vnc", "host")])
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_explicit_target_never_inherits_environment(configured_environment, backend, field, value):
    with pytest.raises(ValueError, match=f"{field} is required"):
        load_backend_config(backend, **{field: value})


def test_explicit_backend_cannot_be_empty(configured_environment):
    with pytest.raises(ValueError, match="backend must"):
        load_backend_config("")


def test_none_inherits_environment_but_explicit_values_override_it(configured_environment):
    inherited = load_backend_config(None, host=None, port=None, username=None, domain=None, width=None, height=None)
    assert isinstance(inherited, RdpConfig)
    assert (inherited.host, inherited.port, inherited.username, inherited.domain, inherited.width, inherited.height) == (
        "env-rdp.example", 3391, "env-user", "ENV-DOMAIN", 1600, 1000,
    )
    explicit = load_backend_config(
        "rdp", host="explicit.example", port=3392, username="explicit-user", domain="", width=1800, height=1200,
    )
    assert isinstance(explicit, RdpConfig)
    assert (explicit.host, explicit.port, explicit.username, explicit.domain, explicit.width, explicit.height) == (
        "explicit.example", 3392, "explicit-user", "", 1800, 1200,
    )
    vnc = load_backend_config("vnc", port=None)
    assert (vnc.host, vnc.port) == ("env-vnc.example", 5901)
    assert load_backend_config("vnc", host="explicit-vnc.example", port=5902).port == 5902


def test_unset_optional_numbers_keep_backend_defaults(monkeypatch):
    for name in ("RDP_PORT", "RDP_WIDTH", "RDP_HEIGHT", "VNC_PORT"):
        monkeypatch.delenv(f"REMOTE_COMPUTER_{name}", raising=False)
    rdp = load_backend_config("rdp", host="rdp.example", username="alice")
    assert isinstance(rdp, RdpConfig)
    assert (rdp.port, rdp.width, rdp.height) == (3389, 1440, 900)
    assert load_backend_config("vnc", host="vnc.example").port == 5900


def test_empty_optional_environment_settings_keep_defaults(monkeypatch):
    for name in ("BACKEND", "RDP_PORT", "RDP_WIDTH", "RDP_HEIGHT", "VNC_PORT"):
        monkeypatch.setenv(f"REMOTE_COMPUTER_{name}", "")
    rdp = load_backend_config(host="rdp.example", username="alice")
    assert isinstance(rdp, RdpConfig)
    assert (rdp.port, rdp.width, rdp.height) == (3389, 1440, 900)
    assert load_backend_config("vnc", host="vnc.example").port == 5900


def test_loads_separate_rdp_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_COMPUTER_BACKEND", "rdp")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_HOST", "rdp.example")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_USERNAME", "operator")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_PASSWORD", "secret")
    monkeypatch.setenv("REMOTE_COMPUTER_RDP_INSECURE_TLS", "true")

    config = load_backend_config()

    assert isinstance(config, RdpConfig)
    assert config.host == "rdp.example"
    assert config.port == 3389
    assert config.password == "secret"
    assert config.insecure_tls is True
    assert "secret" not in repr(config)


def test_loads_separate_vnc_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_COMPUTER_VNC_HOST", "vnc.example")
    monkeypatch.setenv("REMOTE_COMPUTER_VNC_PASSWORD", "secret")
    monkeypatch.setenv("REMOTE_COMPUTER_VNC_SHARED", "false")

    config = load_backend_config("vnc")

    assert isinstance(config, VncConfig)
    assert config.host == "vnc.example"
    assert config.port == 5900
    assert config.password == "secret"
    assert config.shared is False
    assert "secret" not in repr(config)


def test_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="rdp.*vnc"):
        load_backend_config("spice")


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
@pytest.mark.parametrize("environment", [False, True])
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
def test_connection_hosts_preserve_idna2008_and_internal_names(monkeypatch, backend, environment, host, expected):
    if environment:
        monkeypatch.setenv(f"REMOTE_COMPUTER_{backend.upper()}_HOST", host)
    config = load_backend_config(backend, host=None if environment else host, username="alice")
    assert config.host == expected


@pytest.mark.parametrize(
    "host",
    [
        "rdp_service@evil", "vnc_server/path", "win_host?serverip=evil", "host_name#x",
        "host_name%40evil", "host_name:3389", "host_name\\evil", "host_name\x01", "host_name\x80",
        "-host_name", "host_name-", "host_name..local", "x_" + "a" * 62,
        ".".join(["a_" + "a" * 61] * 4), "host＿name", "faß_service.de",
        "fa\u200dss.de", "aא.de", "xn--.example",
    ],
)
def test_internal_name_exception_does_not_bypass_host_safety(host):
    with pytest.raises(ValueError, match="host must"):
        normalize_host(host)


@pytest.mark.parametrize("backend", ["rdp", "vnc"])
@pytest.mark.parametrize("environment", [False, True])
def test_connection_inputs_reject_url_hosts(monkeypatch, backend, environment):
    host = "127.0.0.1@attacker.example"
    if environment:
        monkeypatch.setenv(f"REMOTE_COMPUTER_{backend.upper()}_HOST", host)
    with pytest.raises(ValueError, match="host must"):
        load_backend_config(backend, host=None if environment else host, username="alice")


def test_missing_target_settings_report_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("REMOTE_COMPUTER_"):
            monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=r"(?i)(rdp|vnc|backend).*(required|missing)"):
        load_backend_config()
