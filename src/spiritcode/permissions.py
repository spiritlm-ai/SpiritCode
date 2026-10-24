import asyncio
from pathlib import Path

from spiritcode.config import SECRET_NAMES
from spiritcode.ui import UI


def is_secret(path: Path) -> bool:
    return any(
        part in SECRET_NAMES
        or part.startswith(".env.")
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        or part in {".ssh", ".aws", ".gnupg"}
        for part in path.parts
    )


class Permissions:
    def __init__(self, root: Path, ui: UI) -> None:
        self.root = root.resolve()
        self.ui = ui

    async def path(self, value: str, action: str) -> Path:
        raw = self.root / await asyncio.to_thread(Path(value).expanduser)
        resolved = raw.resolve()
        if ".git" in raw.parts or ".git" in resolved.parts:
            raise PermissionError("Доступ к служебным файлам .git запрещён.")
        reasons = []
        if not resolved.is_relative_to(self.root):
            reasons.append("путь вне проекта")
        if is_secret(raw) or is_secret(resolved):
            reasons.append("файл может содержать секреты")
        if reasons and not await self.ui.approve(
            f"{action}: {resolved}\nПричина: {', '.join(reasons)}",
        ):
            raise PermissionError("Пользователь отклонил доступ.")
        # Revalidate after the human interaction; this is permission control, not a sandbox.
        if raw.resolve() != resolved:
            raise PermissionError("Путь изменился во время подтверждения.")
        return resolved
