import os

import pytest

from app.backends.config import RdpConfig, VncConfig, load_backend_config


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


def test_missing_target_settings_report_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("REMOTE_COMPUTER_"):
            monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=r"(?i)(rdp|vnc|backend).*(required|missing)"):
        load_backend_config()
