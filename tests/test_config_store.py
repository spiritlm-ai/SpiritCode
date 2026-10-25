from unittest.mock import patch

import pytest

from spiritcode.config import Settings
from spiritcode.credentials import Credentials
from spiritcode.store import Store, new_session


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (None, "https://spiritlm.netlify.app"),
        ("", "https://spiritlm.netlify.app"),
        ("production", "https://spiritlm.netlify.app"),
        ("test", "https://spiritlm.netlify.app"),
        ("dev", "http://localhost:3000"),
        ("development", "http://localhost:3000"),
    ],
)
def test_default_server_follows_node_env(tmp_path, monkeypatch, environment, expected):
    monkeypatch.delenv("SPIRITLM_BASE_URL", raising=False)
    if environment is None:
        monkeypatch.delenv("NODE_ENV", raising=False)
    else:
        monkeypatch.setenv("NODE_ENV", environment)
    with patch("spiritcode.config.config_path", return_value=tmp_path / "config.toml"):
        assert Settings().base_url == expected
        assert Settings.load().base_url == expected


def test_saving_model_does_not_pin_environment_server(tmp_path, monkeypatch):
    monkeypatch.delenv("SPIRITLM_BASE_URL", raising=False)
    monkeypatch.setenv("NODE_ENV", "development")
    with patch("spiritcode.config.config_path", return_value=tmp_path / "config.toml"):
        Settings.load().save_model("test-model")
        monkeypatch.delenv("NODE_ENV")
        assert Settings.load().base_url == "https://spiritlm.netlify.app"
        assert Settings.load().model == "test-model"


def test_config_precedence_and_no_keys(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('base_url = "https://file.example"\nmodel = "file"\n')
    monkeypatch.setenv("SPIRITLM_BASE_URL", "https://env.example")
    monkeypatch.setenv("SPIRITLM_MODEL", "env")
    monkeypatch.setenv("NO_COLOR", "")
    with patch("spiritcode.config.config_path", return_value=path):
        assert Settings.load().base_url == "https://env.example"
        settings = Settings.load("https://cli.example", "cli")
        assert settings.model == "cli"
        assert not settings.color
        settings.save_model("новая")
        assert "api_key" not in path.read_text()
        monkeypatch.delenv("SPIRITLM_MODEL")
        assert Settings.load().model == "новая"


def test_keyring_unavailable_memory_and_environment(monkeypatch):
    monkeypatch.delenv("SPIRITLM_API_KEY", raising=False)
    credentials = Credentials("https://example.com")
    with (
        patch("keyring.get_password", side_effect=RuntimeError),
        patch("keyring.set_password", side_effect=RuntimeError),
    ):
        assert credentials.get() is None
        assert not credentials.save("memory-key")
        assert credentials.get() == "memory-key"
        monkeypatch.setenv("SPIRITLM_API_KEY", "env-key")
        assert credentials.get() == "env-key"


def test_session_redacts_keys_and_preserves_money(tmp_path):
    store = Store(tmp_path / "sessions.db")
    session = new_session(tmp_path, "test")
    session["messages"] = [{"role": "user", "content": "sk-spiritlm-private 0.00000001"}]
    store.save(session)
    restored = store.load(session["id"])
    assert "private" not in str(restored)
    assert "0.00000001" in str(restored)
    store.close()


@pytest.mark.parametrize("mode", ["standard", "unrestricted", "full"])
@pytest.mark.parametrize("deletion", [True, False])
def test_permission_settings_round_trip_and_model_changes(tmp_path, mode, deletion):
    with patch("spiritcode.config.config_path", return_value=tmp_path / "config.toml"):
        settings = Settings()
        settings.save_permissions(mode, deletion)
        settings.save_model("next")
        restored = Settings.load()
        assert restored.permission_mode == mode
        assert restored.confirm_deletion is deletion
        assert restored.model == "next"
        restored.save_permissions("standard", True)
        assert Settings.load().model == "next"


@pytest.mark.parametrize(
    "values",
    [
        'permission_mode = "invalid"',
        'confirm_deletion = "false"',
        'permission_mode = ["full"]',
    ],
)
def test_invalid_permission_settings_fail_closed(tmp_path, values):
    path = tmp_path / "config.toml"
    path.write_text(values)
    with patch("spiritcode.config.config_path", return_value=path):
        settings = Settings.load()
        assert settings.permission_mode == "standard"
        assert settings.confirm_deletion is True
        assert settings.permissions_warning
