"""Build, validate and publish SpiritCode using the local .pypirc token.

Run from spiritcode/: uv run python scripts/publish.py
Validate without uploading: uv run python scripts/publish.py --check
Increment src/spiritcode/__init__.py::__version__ before each release.
Requires the development dependencies: uv sync --locked.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Собрать и проверить без публикации")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=ROOT / ".pypirc",
        help="Файл с токеном PyPI (по умолчанию spiritcode/.pypirc)",
    )
    args = parser.parse_args()
    config = args.config_file.expanduser().resolve()
    if not args.check and not config.is_file():
        parser.error(f"Не найден файл {config}")

    # Build into a fresh directory so old releases cannot be uploaded accidentally.
    with tempfile.TemporaryDirectory(prefix="spiritcode-release-") as directory:
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", directory, str(ROOT)],
            cwd=ROOT,
            check=True,
        )
        wheels = list(Path(directory).glob("*.whl"))
        sources = list(Path(directory).glob("*.tar.gz"))
        if len(wheels) != 1 or len(sources) != 1:
            sys.exit("Ожидались один wheel и один sdist.")
        files = [str(path) for path in [*wheels, *sources]]
        subprocess.run(
            [sys.executable, "-m", "twine", "check", "--strict", *files],
            cwd=ROOT,
            check=True,
        )
        if args.check:
            print("Сборка и проверка завершены. Публикация не выполнялась.")
            return
        subprocess.run(
            [
                sys.executable,
                "-m",
                "twine",
                "upload",
                "--non-interactive",
                "--repository",
                "pypi",
                "--config-file",
                str(config),
                *files,
            ],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None
