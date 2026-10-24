"""Check real pip installations in fresh environments without invoking the API."""

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_install(target: str, name: str, *, editable: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix=f"spiritcode-{name}-") as directory:
        work = Path(directory)
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = [str(python), "-m", "pip", "install"]
        if editable:
            command.append("-e")
        subprocess.run([*command, target], cwd=work, check=True)
        version = subprocess.check_output(
            [str(python), "-c", "from spiritcode import __version__; print(__version__)"],
            cwd=work,
            text=True,
        ).strip()
        for executable in ("sprt", "spiritcode"):
            launcher = scripts / (executable + (".exe" if os.name == "nt" else ""))
            actual = subprocess.check_output([str(launcher), "--version"], cwd=work, text=True)
            assert actual.strip() == version, (name, executable, actual)
            subprocess.run([str(launcher), "--help"], cwd=work, check=True)
        subprocess.run([str(python), "-m", "pip", "check"], cwd=work, check=True)
        if editable:
            subprocess.run(
                [
                    str(python),
                    "-c",
                    (
                        "from pathlib import Path; import spiritcode; "
                        f"assert Path(spiritcode.__file__).resolve().is_relative_to({str(ROOT)!r})"
                    ),
                ],
                cwd=work,
                check=True,
            )
        print(f"OK: {name}", flush=True)


def main() -> None:
    wheels = list((ROOT / "dist").glob("*.whl"))
    sources = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        sys.exit("Соберите одну версию wheel и sdist в dist/ перед проверкой.")
    check_install(str(ROOT), "editable", editable=True)
    check_install(str(wheels[0]), "wheel")
    check_install(str(sources[0]), "sdist")


if __name__ == "__main__":
    main()
