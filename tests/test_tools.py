import asyncio
import json
import os
import shlex
import sys

import pytest

from spiritcode.config import Settings
from spiritcode.tools import Tools, project_files
from spiritcode.ui import UI, clean


class FakeUI(UI):
    def __init__(self, approve=True):
        self.approved = approve
        self.prompts = []
        self.output = []

    def say(self, text):
        self.output.append(text)

    def diff(self, text):
        self.output.append(text)

    def notice(self, title, body="", kind="info"):
        self.output.append(title + " " + body)

    def tool(self, title, detail, *, error=False):
        self.output.append(title + " " + detail)

    async def approve(self, description):
        self.prompts.append(description)
        return self.approved


@pytest.fixture
def tools(tmp_path):
    tools = Tools(tmp_path, FakeUI(), [], Settings(permission_mode="full"))
    tools.start_turn()
    return tools


async def call(tools, name, **args):
    return await tools.execute(name, json.dumps(args))


async def test_precise_edits_undo_and_external_changes(tools, tmp_path):
    file = tmp_path / "a.py"
    file.write_text("hello\n", encoding="utf-8")
    assert "Сначала" in await call(
        tools, "edit_file", path="a.py", old_text="hello", new_text="bye"
    )
    await call(tools, "read_file", path="a.py")
    await call(tools, "edit_file", path="a.py", old_text="hello", new_text="bye")
    assert file.read_text() == "bye\n"
    file.write_text("manual\n")
    with pytest.raises(ValueError):
        await tools.undo()
    assert file.read_text() == "manual\n"
    file.write_text("bye\n")
    await tools.undo()
    assert file.read_text() == "hello\n"


async def test_create_multiple_edits_and_group_undo(tools, tmp_path):
    await call(tools, "create_file", path="new.txt", content="a")
    await call(tools, "edit_file", path="new.txt", old_text="a", new_text="b")
    await call(tools, "create_file", path="other.txt", content="c")
    await tools.undo()
    assert not (tmp_path / "new.txt").exists()
    assert not (tmp_path / "other.txt").exists()


async def test_ambiguous_edit_fails(tools, tmp_path):
    (tmp_path / "a").write_text("xx")
    await call(tools, "read_file", path="a")
    result = await call(tools, "edit_file", path="a", old_text="x", new_text="y")
    assert "ровно один" in result


async def test_paths_secret_symlink_and_refusal(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / ".env"
    secret.write_text("PRIVATE")
    ui = FakeUI(False)
    tools = Tools(root, ui, [])
    result = await call(tools, "read_file", path="../.env")
    assert "отклонил" in result
    assert len(ui.prompts) == 1
    try:
        (root / "link").symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks unavailable")
    result = await call(tools, "read_file", path="link")
    assert "PRIVATE" not in result
    assert str(secret) in ui.prompts[-1]
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("PRIVATE")
    assert "запрещён" in await call(tools, "read_file", path=".git/config")


def test_nested_ignore_and_secret_exclusions(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\nignored/\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("!keep.log\n")
    for path in ["a.py", "a.log", ".env", "sub/keep.log", "sub/skip.log"]:
        (tmp_path / path).write_text("x")
    files = {p.relative_to(tmp_path).as_posix() for p in project_files(tmp_path)}
    assert {"a.py", "sub/keep.log"} <= files
    assert not {"a.log", ".env", "sub/skip.log"} & files


async def test_scoped_agents_and_search(tools, tmp_path):
    (tmp_path / "AGENTS.md").write_text("ROOT RULE")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AGENTS.md").write_text("CHILD RULE")
    (tmp_path / "sub" / "file.py").write_text("needle")
    result = await call(tools, "read_file", path="sub/file.py")
    assert "ROOT RULE" in result and "CHILD RULE" in result
    result = await call(tools, "search", query="needle")
    assert "sub/file.py:1:needle" in result


async def test_command_approval_environment_and_output(tools, monkeypatch):
    monkeypatch.setenv("SPIRITLM_API_KEY", "sk-spiritlm-private")
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    command = python + " -c \"import os; print(os.getenv('SPIRITLM_API_KEY', 'absent'))\""
    result = await call(tools, "run_command", command=command, cwd=".")
    assert "absent" in result
    assert "private" not in result
    assert "120" in tools.ui.prompts[0]
    tools.ui.approved = False
    result = await call(tools, "run_command", command="exit 0", cwd=".")
    assert "отклонил" in result


async def test_malformed_arguments_do_not_execute(tools, tmp_path):
    assert "Ошибка" in await tools.execute("create_file", '{"path":"x","content":2}')
    assert not (tmp_path / "x").exists()
    assert "Ошибка" in await tools.execute("other", "{}")


def test_terminal_sanitization():
    assert clean("\x1b[31mred\x1b[0m\x1b]0;title\x07\x00\x9b") == "red"
    assert "private" not in clean("sk-spiritlm-private")


async def test_command_timeout_stops_execution(tools):
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    result = await call(
        tools,
        "run_command",
        command=python + ' -c "import time; time.sleep(30)"',
        cwd=".",
        timeout=1,
    )
    assert "истёк лимит" in result


async def test_command_cancellation_stops_child_tree(tools, tmp_path):
    script = tmp_path / "command.py"
    child = tmp_path / "child.py"
    ready, finished = tmp_path / "ready", tmp_path / "finished"
    child.write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(0.5)\n"
        f"Path({str(finished)!r}).write_text('finished')\n",
    )
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(30)\n",
    )
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    path = f'"{script}"' if os.name == "nt" else shlex.quote(str(script))
    task = asyncio.create_task(call(tools, "run_command", command=f"{python} {path}", cwd="."))
    async with asyncio.timeout(5):
        while not ready.exists():  # noqa: ASYNC110 -- polling a child-process filesystem signal
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.6)
    assert not finished.exists()
