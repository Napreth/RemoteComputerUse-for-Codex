from app.config import Settings


def test_settings_read_the_callers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CUA_DEFAULT_MODEL", "configured-model")
    monkeypatch.setenv("HOST", "127.0.0.2")
    monkeypatch.setenv("PORT", "4141")

    settings = Settings.from_environment()

    assert settings.data_root == tmp_path.resolve()
    assert settings.default_model == "configured-model"
    assert settings.host == "127.0.0.2"
    assert settings.port == 4141
