import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from spiritcode.constants import MAX_FILE_BYTES, MAX_FILES, MAX_RESULT_CHARS
from spiritcode.core.config import Settings
from spiritcode.core.permissions import Permissions, approve_command, is_secret
from spiritcode.sanitize import KEY, clean
from spiritcode.tools.files import project_files, text_file, unified
from spiritcode.tools.schemas import (
    ARGUMENTS,
    TITLES,
    Args,
    CreateFile,
    EditFile,
    ListFiles,
    ReadFile,
    RunCommand,
    Search,
)
from spiritcode.ui.interface import UI


class Tools:
    def __init__(
        self,
        root: Path,
        ui: UI,
        snapshots: list[list[dict[str, Any]]],
        settings: Settings | None = None,
    ) -> None:
        self.root = root.resolve()
        self.ui = ui
        self.snapshots = snapshots
        self.settings = settings if settings is not None else Settings()
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

            if isinstance(args, RunCommand):
                detail = args.command
            else:
                assert isinstance(args, (ListFiles, Search, ReadFile, CreateFile, EditFile))
                detail = args.path
            if isinstance(args, Search):
                detail += " · " + args.query
            self.ui.tool(TITLES[name], detail)

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
        raw = self.root / await asyncio.to_thread(Path(args.cwd).expanduser)
        cwd = raw.resolve()
        if not cwd.is_dir():
            raise ValueError("Рабочая директория не существует.")

        if not await approve_command(
            self.settings, self.ui, self.root, args.command, cwd, args.timeout
        ):
            return "Пользователь отклонил запуск команды."

        if raw.resolve() != cwd:
            raise PermissionError("Путь изменился во время подтверждения.")

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
