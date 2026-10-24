"""Developer defaults and centralized URL/settings handling."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_path, user_data_path

BASE_URL = "http://localhost:3000"
API_PATHS = {
    "models": "/v1/models",
    "metadata": "/v1/spiritcode/models",
    "chat": "/v1/chat/completions",
    "profile": "/v1/account",
    "balance": "/v1/account/balance",
    "subscription": "/v1/account/subscription",
    "usage": "/v1/account/usage",
}
COLORS = {
    "accent": "#ffe02e",
    "text": "#f3f2ee",
    "muted": "#a5a5a1",
    "line": "#30302e",
    "success": "#8fca93",
    "error": "#ff6b6b",
    "info": "#91b9ff",
}
BACKGROUND = "#080808"
STAR = r"""          .-.
         /   \
     .--'     '--.
    /     | |     \
    \             /
     '--.     .--'
         \   /
          '-'"""
REQUEST_TIMEOUT = 100
ACCOUNT_TIMEOUT = 5
COMMAND_TIMEOUT = 120
MAX_ITERATIONS = 30
MAX_FILE_BYTES = 128_000
MAX_RESULT_CHARS = 24_000
MAX_FILES = 2_000
MAX_SSE_EVENT = 1_048_576
FALLBACK_CONTEXT = 16_000
DEFAULT_OUTPUT_TOKENS = 4_096
COMPACTION_THRESHOLD = 0.75
EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".next"}
SECRET_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials"}


def config_path() -> Path:
    return user_config_path("spiritcode") / "config.toml"


def data_path() -> Path:
    return user_data_path("spiritcode")


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parts.scheme not in {"http", "https"}
        or (parts.scheme == "http" and not local)
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("Адрес SpiritLM: HTTPS origin без /v1; HTTP разрешён для localhost.")
    return value.rstrip("/")


@dataclass
class Settings:
    base_url: str = BASE_URL
    model: str | None = None
    color: bool = True

    def url(self, name: str) -> str:
        return self.base_url + API_PATHS[name]

    @classmethod
    def load(cls, base_url: str | None = None, model: str | None = None) -> "Settings":
        path = config_path()
        values = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
        return cls(
            validate_base_url(
                base_url or os.getenv("SPIRITLM_BASE_URL") or values.get("base_url", BASE_URL)
            ),
            model or os.getenv("SPIRITLM_MODEL") or values.get("model"),
            "NO_COLOR" not in os.environ and values.get("color", True),
        )

    def save_model(self, model: str) -> None:
        import json

        self.model = model
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # JSON quoted strings are compatible with TOML basic strings here.
        path.write_text(
            f"base_url = {json.dumps(self.base_url)}\n"
            f"model = {json.dumps(model, ensure_ascii=False)}\n"
            f"color = {str(self.color).lower()}\n",
            encoding="utf-8",
        )
