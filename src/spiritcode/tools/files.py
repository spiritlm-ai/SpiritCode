"""Shared file operations for project discovery, reading, and diffs."""

import difflib
from pathlib import Path

from pathspec import GitIgnoreSpec

from spiritcode.constants import EXCLUDED, MAX_FILE_BYTES, MAX_FILES
from spiritcode.core.permissions import is_secret


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
