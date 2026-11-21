import asyncio
import json
import os
import shlex
import sys

import pytest

from spiritcode.core.config import Settings
from spiritcode.core.store import Store, new_session
from spiritcode.sanitize import clean
from spiritcode.tools.executor import Tools
from spiritcode.tools.files import project_files
from spiritcode.ui.interface import UI


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
    assert "ROOT RULE" not in result and "CHILD RULE" in result
    result = await call(tools, "search", query="needle")
    assert "sub/file.py:1:needle" in result


async def test_scoped_rules_are_refreshed_before_writes(tools, tmp_path):
    directory = tmp_path / "sub"
    directory.mkdir()
    rules = directory / "AGENTS.md"
    rules.write_text("version one")

    args = {"path": "sub/new.txt", "content": "hello"}
    assert "version one" in await call(tools, "list_files", path="sub")
    assert "Запись отложена" in await call(tools, "create_file", **args)
    tools.instructions_delivered()
    rules.write_text("version two")
    assert "version two" in await call(tools, "create_file", **args)
    assert not (directory / "new.txt").exists()
    tools.instructions_delivered()
    assert "Файл изменён" in await call(tools, "create_file", **args)


async def test_batch_changes_round_trip_as_a_separate_undo_group(tmp_path):
    session = new_session(tmp_path, "test")
    ui = FakeUI()
    tools = Tools(tmp_path, ui, session["snapshots"])
    tools.start_turn()
    (tmp_path / "a").write_text("one two")
    (tmp_path / "b").write_text("three")
    for path in ("a", "b"):
        await call(tools, "read_file", path=path)

    await call(tools, "create_file", path="before", content="before")
    edits = [
        {"path": "a", "old_text": "two", "new_text": "SECOND"},
        {"path": "b", "old_text": "three", "new_text": "THIRD"},
        {"path": "a", "old_text": "one", "new_text": "FIRST"},
    ]
    assert "Пакет применён" in await call(tools, "edit_files", edits=edits)
    assert (tmp_path / "a").read_text() == "FIRST SECOND"
    assert (tmp_path / "b").read_text() == "THIRD"
    await call(tools, "create_file", path="after", content="after")

    store = Store(tmp_path / "db")
    try:
        store.save(session)
        restored = store.load(session["id"])
    finally:
        store.close()
    resumed = Tools(tmp_path, ui, restored["snapshots"])
    await resumed.undo()
    assert not (tmp_path / "after").exists()
    (tmp_path / "b").write_text("manual")
    with pytest.raises(ValueError, match="Файл изменён"):
        await resumed.undo()
    assert (tmp_path / "a").read_text() == "FIRST SECOND"
    (tmp_path / "b").write_text("THIRD")
    await resumed.undo()
    assert (tmp_path / "a").read_text() == "one two"
    assert (tmp_path / "b").read_text() == "three"
    assert (tmp_path / "before").exists()
    await resumed.undo()
    assert not (tmp_path / "before").exists()


async def test_batch_validates_all_changes_before_writing(tools, tmp_path):
    (tmp_path / "a").write_text("hello")
    (tmp_path / "b").write_text("world")
    for path in ("a", "b"):
        await call(tools, "read_file", path=path)
    first = {"path": "a", "old_text": "hello", "new_text": "bye"}
    overlap = {"path": "a", "old_text": "ello", "new_text": "x"}
    assert "пересекаются" in await call(tools, "edit_files", edits=[first, overlap])

    second = {"path": "b", "old_text": "world", "new_text": "earth"}
    (tmp_path / "b").write_text("manual")
    assert "Сначала прочитайте" in await call(tools, "edit_files", edits=[first, second])
    tools.ui.approved = False
    outside = {**second, "path": "../outside"}
    assert "отклонил" in await call(tools, "edit_files", edits=[first, outside])
    assert (tmp_path / "a").read_text() == "hello"
    assert (tmp_path / "b").read_text() == "manual"
    assert not tools.snapshots


@pytest.mark.parametrize("fail_restore", [False, True])
async def test_batch_restores_files_when_a_later_replace_fails(
    tools, tmp_path, monkeypatch, fail_restore
):
    for path in ("a", "b"):
        (tmp_path / path).write_text("old")
        await call(tools, "read_file", path=path)
    replace = os.replace

    def fail_second(source, target):
        if target == tmp_path / "b":
            raise OSError("simulated write failure")
        if fail_restore and source.read_text() == "old":
            raise OSError("simulated rollback failure")
        replace(source, target)

    edits = [{"path": path, "old_text": "old", "new_text": "new"} for path in ("a", "b")]
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_second)
        result = await call(tools, "edit_files", edits=edits)
    if fail_restore:
        assert "Не удалось восстановить" in result
        assert (tmp_path / "a").read_text() == "new"
        await tools.undo()
    else:
        assert "simulated write failure" in result
    assert (tmp_path / "a").read_text() == (tmp_path / "b").read_text() == "old"
    assert not tools.snapshots
    assert not list(tmp_path.glob(".spiritcode-*"))


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
