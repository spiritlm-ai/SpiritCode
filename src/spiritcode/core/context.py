import json
import subprocess
from pathlib import Path
from typing import Any

from spiritcode.constants import EXCLUDED, FALLBACK_CONTEXT, MAX_RESULT_CHARS
from spiritcode.prompts import SYSTEM
from spiritcode.sanitize import clean
from spiritcode.tools.files import project_files, text_file


def project_root(directory: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return directory.resolve()


def system_context(root: Path, system_prompt: str = "") -> str:
    files = project_files(root)

    instructions = []
    remaining = MAX_RESULT_CHARS
    root_instructions = root / "AGENTS.md"
    instruction_files = [file for file in files if file.name == "AGENTS.md"]
    if root_instructions.is_file() and not root_instructions.is_symlink():
        instruction_files = [
            root_instructions,
            *[file for file in instruction_files if file != root_instructions],
        ]

    for file in instruction_files:
        body = text_file(file)
        section = f"\nInstructions apply only under {file.parent}:\n{body}\n"
        if len(section) > remaining:
            raise ValueError("AGENTS.md превышают бюджет контекста; сократите инструкции.")
        instructions.append(section)
        remaining -= len(section)

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        status = result.stdout[:4_000]
    except (OSError, subprocess.SubprocessError):
        status = "Git недоступен."

    tree = "\n".join(
        str(path.relative_to(root))
        for path in files[:150]
        if not any(part in EXCLUDED for part in path.parts)
    )

    custom_instructions = (
        f"\nUser-configured instructions:\n{system_prompt}\n" if system_prompt else ""
    )
    return clean(
        SYSTEM
        + custom_instructions
        + f"\nProject root: {root}\n"
        + "".join(instructions)
        + f"\nProject structure (first 150 files):\n{tree}\nGit status:\n{status}"
    )


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    # UTF-8 bytes / 3 is conservative for Cyrillic/code; this is explicitly an estimate.
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) // 3 + 1


def input_budget(context_window: int | None, output_tokens: int) -> int:
    return max(1_000, (context_window or FALLBACK_CONTEXT) - output_tokens)
