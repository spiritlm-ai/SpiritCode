import asyncio
import os
import signal
import subprocess
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any

from spiritcode.constants import EXCLUDED, MAX_FILE_BYTES, MAX_FILES, MAX_RESULT_CHARS
from spiritcode.core.config import Settings
from spiritcode.core.permissions import Permissions, approve_command, is_secret
from spiritcode.sanitize import KEY, clean
from spiritcode.tools.files import project_files, text_file, unified
from spiritcode.tools.replacements import (
    IncompleteRollback,
    Replacement,
    replace_fragments,
    write_replacements,
)
from spiritcode.tools.schemas import (
    ARGUMENTS,
    TITLES,
    Args,
    CreateFile,
    EditFile,
    EditFiles,
    ListFiles,
    ReadFile,
    RunCommand,
    Search,
)
from spiritcode.ui.interface import UI


def validate_edit(path: Path, before: str | None, after: str) -> None:
    # Do not persist secret file contents in undo snapshots.
    if is_secret(path):
        raise PermissionError("Правки секретов не поддерживаются: снимки содержали бы секреты.")
    if KEY.search(str(path)) or KEY.search(before or "") or KEY.search(after):
        raise PermissionError("Файл с API-ключом SpiritLM нельзя сохранять в снимках правок.")
    if len(after.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Новый файл превышает лимит размера.")


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
        self.pending_rules: dict[Path, bytes] = {}
        self.delivered_rules: dict[Path, bytes] = {}

    def start_turn(self) -> None:
        self.group = []
        self.reset_instructions()

    def reset_instructions(self) -> None:
        self.pending_rules.clear()
        self.delivered_rules.clear()

    def instructions_delivered(self) -> None:
        self.delivered_rules.update(self.pending_rules)
        self.pending_rules.clear()

    def scoped_rules(self, directory: Path) -> dict[Path, str]:
        if not directory.is_relative_to(self.root):
            return {}

        rules: dict[Path, str] = {}
        scope = self.root
        for part in directory.relative_to(self.root).parts:
            scope /= part
            if part in EXCLUDED or is_secret(scope):
                break
            agent = scope / "AGENTS.md"
            if not agent.is_symlink() and agent.is_file():
                rules[agent] = text_file(agent)
        return rules

    def expose_rules(self, rules: dict[Path, str], body: str) -> str:
        if not rules:
            return body

        prefix = clean(
            "".join(f"Instructions for {p.parent}:\n{text}\n\n" for p, text in rules.items())
        )
        if len(prefix) >= MAX_RESULT_CHARS:
            raise ValueError("Вложенные AGENTS.md для этого пути превышают лимит результата.")
        self.pending_rules.update(
            (path, sha256(text.encode("utf-8")).digest()) for path, text in rules.items()
        )
        return prefix + body

    def required_rules(self, paths: Iterable[Path]) -> str | None:
        for path in paths:
            rules = self.scoped_rules(path.parent)
            if any(
                self.delivered_rules.get(rule) != sha256(text.encode("utf-8")).digest()
                for rule, text in rules.items()
            ):
                return self.expose_rules(
                    rules, "Запись отложена. Учтите инструкции выше и повторите вызов инструмента."
                )
        return None

    async def execute(self, name: str, arguments: str) -> str:
        try:
            if name not in ARGUMENTS:
                raise ValueError("Неизвестный инструмент.")
            args = ARGUMENTS[name].model_validate_json(arguments)

            if isinstance(args, RunCommand):
                detail = args.command
            elif isinstance(args, EditFiles):
                detail = ", ".join(dict.fromkeys(edit.path for edit in args.edits))
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
        if isinstance(args, EditFiles):
            return await self.edit_files(args)

        assert isinstance(args, (ListFiles, Search, ReadFile, CreateFile, EditFile))
        path = await self.permissions.path(args.path, type(args).__name__)

        if isinstance(args, (ListFiles, Search)):
            # Walk from root so nested .gitignore never loses parent rules.
            base = self.root if path.is_relative_to(self.root) else path
            files = [p for p in project_files(base) if p.is_relative_to(path)]

            if isinstance(args, ListFiles):
                directory = path if path.is_dir() else path.parent
                return self.expose_rules(
                    self.scoped_rules(directory),
                    "\n".join(str(p.relative_to(base)) for p in files)
                    + f"\n[Не более {MAX_FILES} файлов]",
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
            lines = body.splitlines()
            result = self.expose_rules(
                self.scoped_rules(path.parent),
                "\n".join(
                    f"{i}: {line}" for i, line in enumerate(lines, 1) if args.start <= i <= args.end
                )
                + ("\n[Есть следующие строки]" if len(lines) > args.end else ""),
            )
            self.reads[path] = body
            return result

        if instructions := self.required_rules([path]):
            return instructions

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

        validate_edit(path, before, after)

        path.parent.mkdir(parents=True, exist_ok=True)
        if before is None:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(after)
        else:
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(after)

        self.reads[path] = after
        if not self.group:
            self.snapshots.append(self.group)
        self.group.append({"path": str(path), "before": before, "after": after})

        diff = unified(str(path), before, after)
        self.ui.diff(diff)
        return "Файл изменён.\n" + diff

    async def edit_files(self, args: EditFiles) -> str:
        resolved: dict[str, tuple[Path, Path]] = {}
        edits: dict[Path, list[EditFile]] = {}
        for edit in args.edits:
            if edit.path not in resolved:
                resolved[edit.path] = await self.permissions.checked_path(edit.path, "Пакет правок")
            _, path = resolved[edit.path]
            edits.setdefault(path, []).append(edit)

        for raw, path in resolved.values():
            if raw.resolve() != path or path.resolve() != path:
                raise PermissionError("Путь изменился во время подтверждения пакета.")

        if instructions := self.required_rules(edits):
            return instructions

        replacements: list[Replacement] = []
        for path, fragments in edits.items():
            aliases = tuple(dict.fromkeys(resolved[fragment.path][0] for fragment in fragments))
            if is_secret(path) or any(is_secret(alias) for alias in aliases):
                raise PermissionError("Пакетные правки секретов не поддерживаются.")
            if not path.is_file():
                raise ValueError(f"Пакетная правка требует существующий файл: {path}")
            before = text_file(path)
            if self.reads.get(path) != before:
                raise ValueError(f"Сначала прочитайте актуальный файл через read_file: {path}")
            after = replace_fragments(
                before, ((edit.old_text, edit.new_text) for edit in fragments)
            )
            validate_edit(path, before, after)
            if "\0" in after:
                raise ValueError(f"Правка создаёт бинарный файл: {path}")
            replacements.append(Replacement(path, before, after, aliases))

        try:
            write_replacements(replacements)
        except IncompleteRollback as exc:
            pending = [item for item in replacements if item.path in exc.paths]
            self.record_batch(pending)
            for item in pending:
                self.reads.pop(item.path, None)
            raise

        self.record_batch(replacements)
        diffs = []
        for item in replacements:
            self.reads[item.path] = item.after
            diff = unified(str(item.path), item.before, item.after)
            self.ui.diff(diff)
            diffs.append(diff)
        return f"Пакет применён. Файлов: {len(replacements)}.\n" + "\n".join(diffs)

    def record_batch(self, replacements: list[Replacement]) -> None:
        self.snapshots.append(
            [
                {"path": str(item.path), "before": item.before, "after": item.after, "batch": True}
                for item in replacements
            ]
        )
        self.group = []

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
        index = next((i for i in reversed(range(len(self.snapshots))) if self.snapshots[i]), None)
        if index is None:
            return "Нет правок для отмены. Команды не откатываются."
        group = self.snapshots[index]

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

        if group[0].get("batch"):
            replacements = [
                Replacement(Path(change["path"]), change["after"], change["before"])
                for change in group
            ]
            try:
                write_replacements(replacements)
            except IncompleteRollback as exc:
                group[:] = [change for change in group if Path(change["path"]) not in exc.applied]
                self.reads.clear()
                return f"Отмена пакета завершилась частично. {exc}"
        else:
            restore()
        del self.snapshots[index]
        if self.group is group:
            self.group = []
        self.reads.clear()
        return "Последняя группа правок отменена. Команды не откатывались."
