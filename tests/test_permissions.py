from unittest.mock import AsyncMock, patch

import pytest

from spiritcode.config import Settings
from spiritcode.permissions import command_risk
from spiritcode.tools import Tools
from tests.test_tools import FakeUI, call


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git push",
        "rm -rf out",
        "rmdir out",
        "del a",
        "Remove-Item a",
        "echo x > a",
        "cp a b",
        "mv a b",
        "sudo reboot",
        "echo ok && rm a",
        "cat a | sh",
        "echo $(rm a)",
        'bash -c "rm a"',
        "python -c \"import shutil; shutil.rmtree('a')\"",
        "rg --pre=sh x",
        'git -c alias.x="!rm a" x',
        "echo `rm a`",
        "echo x\nrm a",
    ],
)
async def test_standard_denies_risky_commands_before_spawn(tmp_path, command):
    ui = FakeUI(False)
    tools = Tools(tmp_path, ui, [], Settings())
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
        assert "отклонил" in await call(tools, "run_command", command=command, cwd=".")
    spawn.assert_not_called()
    assert len(ui.prompts) == 1
    assert command in ui.prompts[0]
    assert str(tmp_path) in ui.prompts[0]


@pytest.mark.parametrize(
    "mode,confirm,command,prompt",
    [
        ("standard", True, "echo checked", False),
        ("standard", False, "git status", True),
        ("full", False, "echo checked", True),
        ("unrestricted", True, "echo checked", False),
        ("unrestricted", True, "rm file", True),
        ("unrestricted", True, "python script.py", True),
        ("unrestricted", False, "rm file", False),
        ("unrestricted", False, "echo x > file", False),
    ],
)
async def test_permission_matrix(tmp_path, mode, confirm, command, prompt):
    ui = FakeUI(False)
    tools = Tools(tmp_path, ui, [], Settings(permission_mode=mode, confirm_deletion=confirm))
    # Simulate only the process boundary; denied commands must never reach it.
    with patch("asyncio.create_subprocess_shell", side_effect=OSError("spawn reached")) as spawn:
        result = await call(tools, "run_command", command=command, cwd=".")
    assert bool(ui.prompts) == prompt
    assert spawn.call_count == (0 if prompt else 1)
    assert ("отклонил" if prompt else "spawn reached") in result


@pytest.mark.parametrize("command", ["rm a", "del a", "Remove-Item a", "git clean -fd"])
def test_deletion_recognition(command):
    assert command_risk(command).deletion


async def test_cwd_checked_and_confirmed_once(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    ui = FakeUI(False)
    tools = Tools(root, ui, [], Settings())
    assert "отклонил" in await call(tools, "run_command", command="git status", cwd="..")
    assert len(ui.prompts) == 1
    assert "вне проекта" in ui.prompts[0]
    assert "не существует" in await call(tools, "run_command", command="echo x", cwd="missing")


async def test_setting_changes_apply_to_existing_tools(tmp_path):
    settings = Settings()
    ui = FakeUI(False)
    tools = Tools(tmp_path, ui, [], settings)
    assert "checked" in await call(tools, "run_command", command="echo checked", cwd=".")
    settings.permission_mode = "full"
    assert "отклонил" in await call(tools, "run_command", command="echo checked", cwd=".")
    assert len(ui.prompts) == 1


@pytest.mark.parametrize(
    "command",
    ["cat .env", "cat ../private", "echo %COMMAND%", "echo !COMMAND!", "rg --pre sh needle"],
)
async def test_standard_requires_approval_for_sensitive_or_expanded_commands(tmp_path, command):
    ui = FakeUI(False)
    tools = Tools(tmp_path, ui, [], Settings())
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
        assert "отклонил" in await call(tools, "run_command", command=command, cwd=".")
    spawn.assert_not_called()


async def test_unrestricted_executes_deletion_only_with_checkbox_off(tmp_path):
    import os
    import shlex
    import sys

    file = tmp_path / "temporary.txt"
    file.write_text("test")
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    command = python + " -c \"from pathlib import Path; Path('temporary.txt').unlink()\""
    settings = Settings(permission_mode="unrestricted")
    ui = FakeUI(False)
    tools = Tools(tmp_path, ui, [], settings)
    assert "отклонил" in await call(tools, "run_command", command=command, cwd=".")
    assert file.exists()
    settings.confirm_deletion = False
    assert "Код выхода: 0" in await call(tools, "run_command", command=command, cwd=".")
    assert not file.exists()
    assert len(ui.prompts) == 1
