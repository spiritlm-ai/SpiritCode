import asyncio
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from spiritcode.config import COMMAND_TIMEOUT, SECRET_NAMES, Settings
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


@dataclass(frozen=True)
class CommandRisk:
    reason: str = ""
    deletion: bool = False
    opaque: bool = False


def command_risk(command: str) -> CommandRisk:
    """Conservative recognition, not a shell parser or an OS sandbox."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return CommandRisk("Не удалось разобрать команду", opaque=True)
    if not tokens:
        return CommandRisk("Пустая команда", opaque=True)
    names = {token.replace("\\", "/").rsplit("/", 1)[-1].lower() for token in tokens}
    names = {name.removesuffix(".exe") for name in names}
    deletion = bool(names & {"rm", "rmdir", "del", "erase", "remove-item", "rd", "unlink", "shred"})
    deletion |= "git" in names and bool(names & {"clean", "reset", "restore", "checkout"})
    if deletion:
        return CommandRisk("Удаление или разрушительная замена файлов", deletion=True)
    if "git" in names:
        return CommandRisk("Команда Git", opaque=True)
    # Even quoted metacharacters are treated conservatively; never approve a
    # compound command merely because its first executable looks harmless.
    if re.search(r"[;&|<>`$(){}%!^\\\n\r]", command):
        return CommandRisk("Составная команда, перенаправление или подстановка", opaque=True)
    executable = tokens[0].lower()
    if executable in {
        "pwd",
        "ls",
        "dir",
        "cat",
        "head",
        "tail",
        "wc",
        "rg",
        "grep",
        "echo",
        "printf",
        "cd",
    }:
        # File/argument loading options can make otherwise read-only programs
        # execute code or write output. Restrict the automatic subset.
        if executable in {"rg", "grep"} and any(
            t.startswith(("--pre", "--hostname-bin")) for t in tokens[1:]
        ):
            return CommandRisk("Внешняя обработка результатов поиска", opaque=True)
        return CommandRisk()
    if tokens in (
        ["npm", "test"],
        ["npm", "run", "test:unit"],
        ["npm", "run", "typecheck"],
        ["npm", "run", "lint"],
        ["pytest"],
        ["ruff", "check"],
        ["mypy", "."],
    ):
        return CommandRisk()
    return CommandRisk("Изменяющая или непрозрачная команда", opaque=True)


async def approve_command(
    settings: Settings, ui: UI, root: Path, command: str, cwd: Path, timeout_seconds: int
) -> bool:
    risk = command_risk(command)
    reasons: list[str] = []
    if settings.permission_mode == "full":
        reasons.append("Полная защита")
    elif settings.permission_mode == "standard":
        if timeout_seconds > COMMAND_TIMEOUT:
            reasons.append("Увеличенный лимит времени")
        if risk.reason:
            reasons.append(risk.reason)
        if not cwd.is_relative_to(root):
            reasons.append("Каталог вне проекта")
        try:
            arguments = shlex.split(command)[1:]
        except ValueError:
            arguments = []
        for argument in arguments:
            if is_secret(Path(argument)):
                reasons.append("Аргумент может указывать на секреты")
                break
            if argument.startswith(("/", "../", "~/")):
                reasons.append("Аргумент может указывать на путь вне проекта")
                break
        if is_secret(cwd):
            reasons.append("Каталог может содержать секреты")
    elif settings.confirm_deletion and (risk.deletion or risk.opaque):
        reasons.append("Возможное удаление: " + risk.reason)
    if not reasons:
        return True
    return await ui.approve(
        f"Команда: {command}\nКаталог: {cwd}\nТаймаут: {timeout_seconds} с\n"
        + "Причина: "
        + "; ".join(reasons)
    )
