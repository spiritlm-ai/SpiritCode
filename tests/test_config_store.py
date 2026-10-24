from unittest.mock import patch

from spiritcode.config import Settings
from spiritcode.credentials import Credentials
from spiritcode.store import Store, new_session


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
