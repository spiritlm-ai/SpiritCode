import asyncio
import difflib
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from pathspec import GitIgnoreSpec
from pydantic import BaseModel, ConfigDict, Field

from spiritcode.config import (
    COMMAND_TIMEOUT,
    EXCLUDED,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_RESULT_CHARS,
)
from spiritcode.permissions import Permissions, is_secret
from spiritcode.ui import KEY, UI, clean


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListFiles(Args):
    path: str = "."


class Search(Args):
    query: str = Field(min_length=1, max_length=1_000)
    path: str = "."


class ReadFile(Args):
    path: str
    start: int = Field(default=1, ge=1)
    end: int = Field(default=250, ge=1)


class CreateFile(Args):
    path: str
    content: str = Field(max_length=MAX_FILE_BYTES)


class EditFile(Args):
    path: str
    old_text: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    new_text: str = Field(max_length=MAX_FILE_BYTES)


class RunCommand(Args):
    command: str = Field(min_length=1, max_length=10_000)
    cwd: str
    timeout: int = Field(default=COMMAND_TIMEOUT, ge=1, le=3_600)


ARGUMENTS: dict[str, type[Args]] = {
    "list_files": ListFiles,
    "search": Search,
    "read_file": ReadFile,
    "create_file": CreateFile,
    "edit_file": EditFile,
    "run_command": RunCommand,
}
DESCRIPTIONS = {
    "list_files": "List project files, respecting ignore rules. Never follows directory symlinks.",
    "search": "Search literal text across project files, respecting ignore rules.",
    "read_file": "Read UTF-8 file lines (inclusive, 1-based), and scoped AGENTS.md instructions.",
    "create_file": "Create a new UTF-8 file. Fails if a file already exists.",
    "edit_file": "Replace exactly one occurrence of old_text. Read the file first.",
    "run_command": "Run shell command in explicit cwd after user approval. Not a sandbox.",
}


def definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DESCRIPTIONS[name],
                "parameters": model.model_json_schema(),
            },
        }
        for name, model in ARGUMENTS.items()
    ]


def text_file(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"Файл превышает лимит {MAX_FILE_BYTES} байт.")
    if b"\0" in raw:
        raise ValueError("Бинарный файл пропущен.")
    return raw.decode("utf-8")


def project_files(root: Path) -> list[Path]:
    """Per-directory GitIgnoreSpec preserves nested rules without requiring Git or rg."""
    result: list[Path] = []

    def walk(directory: Path, rules: list[tuple[Path, GitIgnoreSpec]]) -> None:
        ignore = directory / ".gitignore"
        if ignore.is_file() and not ignore.is_symlink():
            try:
                rules = [
                    *rules,
                    (directory, GitIgnoreSpec.from_lines(text_file(ignore).splitlines())),
                ]
            except (OSError, UnicodeError, ValueError):
                pass
        for child in sorted(directory.iterdir()):
            if len(result) >= MAX_FILES:
                return
            if child.name in EXCLUDED or is_secret(child) or child.is_symlink():
                continue
            ignored = False
            for base, spec in rules:
                relative = child.relative_to(base).as_posix() + ("/" if child.is_dir() else "")
                match = spec.check_file(relative)
                if match.include is not None:
                    ignored = match.include
            if ignored:
                continue
            if child.is_dir():
                try:
                    walk(child, rules)
                except OSError:
                    continue
            elif child.is_file():
                result.append(child)

    walk(root, [])
    return result


def unified(path: str, before: str | None, after: str | None) -> str:
    return "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class Tools:
    def __init__(self, root: Path, ui: UI, snapshots: list[list[dict[str, Any]]]) -> None:
        self.root, self.ui, self.snapshots = root.resolve(), ui, snapshots
        self.permissions = Permissions(root, ui)
        self.group: list[dict[str, Any]] = []
        self.reads: dict[Path, str] = {}

    def start_turn(self) -> None:
        self.group = []
        self.snapshots.append(self.group)

    def instructions(self, path: Path) -> str:
        if not path.is_relative_to(self.root):
            return ""
        sections: list[tuple[Path, str]] = []
        for directory in [self.root, *reversed(path.parent.parents), path.parent]:
            if not directory.is_relative_to(self.root):
                continue
            agent = directory / "AGENTS.md"
            if agent.is_file() and not agent.is_symlink() and agent not in [s[0] for s in sections]:
                sections.append((agent, text_file(agent)))
        return "\n".join(f"Instructions for {p.parent}:\n{body}" for p, body in sections)

    async def execute(self, name: str, arguments: str) -> str:
        try:
            if name not in ARGUMENTS:
                raise ValueError("Неизвестный инструмент.")
            args = ARGUMENTS[name].model_validate_json(arguments)
            titles = {
                "list_files": "Файлы проекта",
                "search": "Поиск",
                "read_file": "Чтение",
                "create_file": "Создание",
                "edit_file": "Изменение",
                "run_command": "Команда",
            }
            if isinstance(args, RunCommand):
                detail = args.command
            else:
                assert isinstance(args, (ListFiles, Search, ReadFile, CreateFile, EditFile))
                detail = args.path
            if isinstance(args, Search):
                detail += " · " + args.query
            self.ui.tool(titles[name], detail)
            result = await self.dispatch(args)
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "\n[Результат усечён по лимиту]"
            return clean(result)
        except (ValueError, OSError, PermissionError) as exc:
            self.ui.tool("Ошибка инструмента", str(exc), error=True)
            return clean(f"Ошибка инструмента: {exc}")

    async def dispatch(self, args: Args) -> str:
        if isinstance(args, RunCommand):
            return await self.run(args)
        assert isinstance(args, (ListFiles, Search, ReadFile, CreateFile, EditFile))
        path = await self.permissions.path(args.path, type(args).__name__)
        if isinstance(args, (ListFiles, Search)):
            # Walk from root so nested .gitignore never loses parent rules.
            base = self.root if path.is_relative_to(self.root) else path
            files = [p for p in project_files(base) if p.is_relative_to(path)]
            if isinstance(args, ListFiles):
                return "\n".join(str(p.relative_to(base)) for p in files) + (
                    f"\n[Не более {MAX_FILES} файлов]"
                )
            matches = []
            for file in files:
                try:
                    for number, line in enumerate(text_file(file).splitlines(), 1):
                        if args.query in line:
                            matches.append(f"{file.relative_to(base)}:{number}:{line}")
                            if sum(map(len, matches)) >= MAX_RESULT_CHARS:
                                return "\n".join(matches) + "\n[Поиск усечён]"
                except (OSError, UnicodeError, ValueError):
                    continue
            return "\n".join(matches) or "Совпадений нет."
        if isinstance(args, ReadFile):
            if args.end < args.start:
                raise ValueError("Конец диапазона должен быть не меньше начала.")
            body = text_file(path)
            self.reads[path] = body
            lines = body.splitlines()
            return (
                self.instructions(path)
                + "\n"
                + "\n".join(
                    f"{i}: {line}" for i, line in enumerate(lines, 1) if args.start <= i <= args.end
                )
                + ("\n[Есть следующие строки]" if len(lines) > args.end else "")
            )
        before = text_file(path) if path.exists() else None
        if isinstance(args, CreateFile):
            if before is not None:
                raise ValueError("Файл уже существует; используйте edit_file.")
            after = args.content
        else:
            assert isinstance(args, EditFile)
            if before is None or self.reads.get(path) != before:
                raise ValueError("Сначала прочитайте актуальный файл через read_file.")
            if before.count(args.old_text) != 1:
                raise ValueError("Исходный текст должен совпадать ровно один раз.")
            after = before.replace(args.old_text, args.new_text, 1)
        # Do not persist secret file contents in undo snapshots.
        if is_secret(path):
            raise PermissionError("Правки секретов не поддерживаются: снимки содержали бы секреты.")
        if KEY.search(before or "") or KEY.search(after):
            raise PermissionError("Файл с API-ключом SpiritLM нельзя сохранять в снимках правок.")
        if len(after.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("Новый файл превышает лимит размера.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if before is None:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(after)
        else:
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(after)
        self.reads[path] = after
        self.group.append({"path": str(path), "before": before, "after": after})
        diff = unified(str(path), before, after)
        self.ui.diff(diff)
        return "Файл изменён.\n" + diff

    async def run(self, args: RunCommand) -> str:
        cwd = await self.permissions.path(args.cwd, "Рабочая директория команды")
        description = f"Команда: {args.command}\nКаталог: {cwd}\nТаймаут: {args.timeout} с"
        if args.timeout > COMMAND_TIMEOUT:
            description += "\nУвеличенный лимит времени."
        if not await self.ui.approve(description):
            return "Пользователь отклонил запуск команды."
        env = {k: v for k, v in os.environ.items() if k.upper() != "SPIRITLM_API_KEY"}
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            options["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            args.command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **options,
        )
        output = bytearray()
        truncated = False

        async def collect() -> None:
            nonlocal truncated
            assert process.stdout
            while chunk := await process.stdout.read(4096):
                remaining = MAX_RESULT_CHARS - len(output)
                output.extend(chunk[: max(0, remaining)])
                truncated |= len(chunk) > remaining
            await process.wait()

        try:
            async with asyncio.timeout(args.timeout):
                await collect()
        except TimeoutError:
            await self.stop(process)
            return "Команда остановлена: истёк лимит времени."
        except asyncio.CancelledError:
            await self.stop(process)
            raise
        return (
            f"Код выхода: {process.returncode}\n"
            + output.decode("utf-8", errors="replace")
            + ("\n[Вывод усечён]" if truncated else "")
        )

    @staticmethod
    async def stop(process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def undo(self) -> str:
        group = next((group for group in reversed(self.snapshots) if group), None)
        if not group:
            return "Нет правок для отмены. Команды не откатываются."
        expected: dict[Path, str | None] = {}
        for change in group:
            expected[Path(change["path"])] = change["after"]
        for path, content in expected.items():
            checked = await self.permissions.path(str(path), "Отмена правки")
            if checked != path or not path.exists() or text_file(path) != content:
                raise ValueError("Файл изменён после правки; отмена остановлена.")

        def restore() -> None:
            for change in reversed(group):
                path = Path(change["path"])
                if change["before"] is None:
                    path.unlink()
                else:
                    with path.open("w", encoding="utf-8", newline="") as stream:
                        stream.write(change["before"])

        restore()
        self.snapshots.remove(group)
        self.reads.clear()
        return "Последняя группа правок отменена. Команды не откатывались."
