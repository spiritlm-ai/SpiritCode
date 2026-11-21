"""Tool argument schemas and metadata exposed to the model."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spiritcode.constants import COMMAND_TIMEOUT, MAX_BATCH_EDITS, MAX_FILE_BYTES


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


class EditFiles(Args):
    edits: list[EditFile] = Field(min_length=1, max_length=MAX_BATCH_EDITS)


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
    "edit_files": EditFiles,
    "run_command": RunCommand,
}

DESCRIPTIONS = {
    "list_files": "List project files respecting ignore rules, and scoped AGENTS.md for the path.",
    "search": "Search literal text across project files, respecting ignore rules.",
    "read_file": "Read UTF-8 file lines (inclusive, 1-based), and scoped AGENTS.md instructions.",
    "create_file": "Create a new UTF-8 file. Fails if a file already exists.",
    "edit_file": "Replace exactly one occurrence of old_text. Read the file first.",
    "edit_files": (
        "Apply a batch of non-overlapping exact replacements to existing files as one undo group. "
        "Read every file first. All old_text values refer to the original file contents."
    ),
    "run_command": (
        "Run any shell command in explicit cwd under the active permission policy. Not a sandbox."
    ),
}

TITLES = {
    "list_files": "Файлы проекта",
    "search": "Поиск",
    "read_file": "Чтение",
    "create_file": "Создание",
    "edit_file": "Изменение",
    "edit_files": "Пакет правок",
    "run_command": "Команда",
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
