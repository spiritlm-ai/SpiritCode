import os

import keyring


class Credentials:
    def __init__(self, origin: str) -> None:
        self.service = f"SpiritLM:{origin}"
        self.memory: str | None = None

    def get(self) -> str | None:
        if key := os.getenv("SPIRITLM_API_KEY") or self.memory:
            return key
        try:
            return keyring.get_password(self.service, "api_key")
        except Exception:
            return None

    def save(self, key: str) -> bool:
        self.memory = key
        try:
            keyring.set_password(self.service, "api_key", key)
            return True
        except Exception:
            return False

    def delete(self) -> bool:
        self.memory = None
        try:
            keyring.delete_password(self.service, "api_key")
            return True
        except keyring.errors.PasswordDeleteError:
            return True
        except Exception:
            return False
