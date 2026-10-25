"""Developer defaults and centralized URL/settings handling."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from platformdirs import user_config_path, user_data_path

BASE_URL = "https://spiritlm.netlify.app"
DEV_BASE_URL = "http://localhost:3000"
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
# Deployment context window confirmed by the product owner. Server metadata takes precedence.
MODEL_CONTEXT_WINDOWS = {
    "anthropic/claude-haiku-5.5": 1_000_000,
    "claude-haiku-5.5": 1_000_000,
}
FALLBACK_CONTEXT = 16_000
DEFAULT_OUTPUT_TOKENS = 4_096
COMPACTION_THRESHOLD = 0.75
EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".next"}
SECRET_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials"}


def config_path() -> Path:
    return user_config_path("spiritcode") / "config.toml"


def data_path() -> Path:
    return user_data_path("spiritcode")


def default_base_url() -> str:
    if os.getenv("NODE_ENV") in {"dev", "development"}:
        return DEV_BASE_URL
    return BASE_URL


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


PermissionMode = Literal["standard", "unrestricted", "full"]
PERMISSION_MODES: tuple[PermissionMode, ...] = ("full", "standard", "unrestricted")


@dataclass
class Settings:
    base_url: str = field(default_factory=default_base_url)
    model: str | None = None
    color: bool = True
    permission_mode: PermissionMode = "standard"
    confirm_deletion: bool = True
    permissions_warning: bool = False

    def url(self, name: str) -> str:
        return self.base_url + API_PATHS[name]

    @classmethod
    def load(cls, base_url: str | None = None, model: str | None = None) -> "Settings":
        path = config_path()
        values = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
        mode = values.get("permission_mode", "standard")
        deletion = values.get("confirm_deletion", True)
        invalid = mode not in PERMISSION_MODES or not isinstance(deletion, bool)
        return cls(
            validate_base_url(
                base_url
                or os.getenv("SPIRITLM_BASE_URL")
                or values.get("base_url", default_base_url())
            ),
            model or os.getenv("SPIRITLM_MODEL") or values.get("model"),
            "NO_COLOR" not in os.environ and values.get("color", True),
            "standard" if invalid else mode,
            True if invalid else deletion,
            invalid,
        )

    def save_model(self, model: str) -> None:
        self._save(model=model)

    def save_permissions(self, mode: PermissionMode, confirm_deletion: bool) -> None:
        self._save(permission_mode=mode, confirm_deletion=confirm_deletion)
        self.permissions_warning = False

    def _save(
        self,
        *,
        model: str | None = None,
        permission_mode: PermissionMode | None = None,
        confirm_deletion: bool | None = None,
    ) -> None:
        import json

        model = model if model is not None else self.model
        mode = permission_mode if permission_mode is not None else self.permission_mode
        deletion = confirm_deletion if confirm_deletion is not None else self.confirm_deletion
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep automatic environment selection active after changing models.
        server_config = (
            f"base_url = {json.dumps(self.base_url)}\n"
            if self.base_url != default_base_url()
            else ""
        )
        # JSON quoted strings are compatible with TOML basic strings here.
        path.write_text(
            server_config
            + (f"model = {json.dumps(model, ensure_ascii=False)}\n" if model else "")
            + f"color = {str(self.color).lower()}\n"
            + f"permission_mode = {json.dumps(mode)}\n"
            + f"confirm_deletion = {str(deletion).lower()}\n",
            encoding="utf-8",
        )
        self.model = model
        self.permission_mode = mode
        self.confirm_deletion = deletion
