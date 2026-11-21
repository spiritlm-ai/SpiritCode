"""Exact replacements and recoverable writes for a batch of existing files."""

import os
import stat
from collections.abc import Iterable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from spiritcode.tools.files import text_file


@dataclass(frozen=True)
class Replacement:
    path: Path
    before: str
    after: str
    aliases: tuple[Path, ...] = ()


class IncompleteRollback(OSError):
    def __init__(self, paths: list[Path], applied: set[Path]) -> None:
        self.paths = paths
        self.applied = applied
        super().__init__("Не удалось восстановить файлы: " + ", ".join(map(str, paths)))


def replace_fragments(original: str, edits: Iterable[tuple[str, str]]) -> str:
    ranges: list[tuple[int, int, str]] = []
    for old, new in edits:
        start = original.find(old)
        if start < 0 or original.find(old, start + 1) >= 0:
            raise ValueError("Исходный текст должен совпадать ровно один раз.")
        ranges.append((start, start + len(old), new))

    parts: list[str] = []
    offset = 0
    for start, end, new in sorted(ranges):
        if start < offset:
            raise ValueError("Диапазоны замен пересекаются.")
        parts.extend((original[offset:start], new))
        offset = end
    parts.append(original[offset:])
    return "".join(parts)


def _check(replacement: Replacement, expected: os.stat_result, content: str) -> None:
    path = replacement.path
    if path.resolve() != path or any(alias.resolve() != path for alias in replacement.aliases):
        raise ValueError(f"Путь изменился во время правки: {path}")
    current = path.stat()
    if (
        current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
        or current.st_mode != expected.st_mode
        or current.st_nlink != 1
        or text_file(path) != content
    ):
        raise ValueError(f"Файл изменился во время правки: {path}")


def _remove_temp(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def _stage(stack: ExitStack, path: Path, content: str, mode: int) -> Path:
    with NamedTemporaryFile(dir=path.parent, prefix=".spiritcode-", delete=False) as stream:
        staged = Path(stream.name)
        stack.callback(_remove_temp, staged)
        stream.write(content.encode("utf-8"))
    staged.chmod(mode)
    return staged


def write_replacements(replacements: list[Replacement]) -> None:
    with ExitStack() as stack:
        staged: list[tuple[Replacement, os.stat_result, Path, Path, os.stat_result]] = []
        for replacement in replacements:
            path = replacement.path
            original = path.stat()
            if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
                raise ValueError(
                    f"Для пакетной правки нужен обычный файл без жёстких ссылок: {path}"
                )
            _check(replacement, original, replacement.before)
            # Replacing an entry only needs directory permissions on POSIX.
            with path.open("r+b"):
                pass
            mode = stat.S_IMODE(original.st_mode)
            backup = _stage(stack, path, replacement.before, mode)
            updated = _stage(stack, path, replacement.after, mode)
            staged.append((replacement, original, backup, updated, updated.stat()))

        committed: list[tuple[Replacement, Path, os.stat_result]] = []
        try:
            for replacement, original, backup, updated, new_stat in staged:
                _check(replacement, original, replacement.before)
                os.replace(updated, replacement.path)
                committed.append((replacement, backup, new_stat))
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
            remaining = []
            applied = set()
            for replacement, backup, new_stat in reversed(committed):
                try:
                    target = Replacement(replacement.path, replacement.after, replacement.before)
                    _check(target, new_stat, target.before)
                    os.replace(backup, target.path)
                except (OSError, ValueError, RuntimeError):
                    remaining.append(replacement.path)
                    with suppress(OSError, ValueError, RuntimeError):
                        _check(target, new_stat, target.before)
                        applied.add(target.path)
            if remaining:
                raise IncompleteRollback(remaining, applied) from exc
            raise
