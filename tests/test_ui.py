import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.history import DummyHistory
from rich.console import Console

from spiritcode.ui import UI


def test_extended_insert_and_ctrl_c_are_keys_instead_of_literal_sequences():
    from prompt_toolkit.keys import Keys

    from spiritcode.keyboard import KeyboardParser

    keys = []
    parser = KeyboardParser(keys.append)
    parser.feed("\x1b[57410u\x1b[99;5u")
    parser.flush()
    assert [key.key for key in keys] == ["/", Keys.ControlC]


def test_extended_keyboard_decodes_escape_editing_keys_and_keeps_paste_literal():
    from prompt_toolkit.keys import Keys

    from spiritcode.keyboard import KeyboardParser

    keys = []
    parser = KeyboardParser(keys.append)
    # Split packets as real terminal input may arrive across multiple reads.
    parser.feed("\x1b[27u\x1b[97;")
    parser.feed("5u\x1b[98;3u\x1b[57400u\x1b[13;2u")
    parser.feed("\x1b[200~\x1b[99;5u\x1b[201~")
    parser.flush()
    assert [key.key for key in keys] == [
        Keys.Escape,
        Keys.ControlA,
        Keys.Escape,
        "b",
        "1",
        Keys.ControlO,
        Keys.BracketedPaste,
    ]
    assert keys[-1].data == "\x1b[99;5u"


def test_chat_prompt_is_erased_before_next_prompt():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
    ):
        ui = UI(color=False)
        assert ui.prompt is not None
        assert ui.prompt.app.erase_when_done


async def test_extended_keys_work_in_chat_and_restore_keyboard_protocol():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import DummyCompleter
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.ui import input_bindings

    ui = UI(color=False)
    output = DummyOutput()
    with create_pipe_input() as pipe, patch.object(output, "write_raw") as write_raw:
        ui.prompt = PromptSession(
            input=pipe, output=output, key_bindings=input_bindings(ui), multiline=True
        )
        pipe.send_text("\x1b[57410uhelp\r")
        assert await ui.ask(chat=True, completer=DummyCompleter()) == "/help"
        write_raw.assert_any_call("\x1b[>1u")
        write_raw.assert_any_call("\x1b[<u")
        pipe.send_text("text\x1b[99;5u")
        with pytest.raises(KeyboardInterrupt):
            await ui.ask(chat=True)
        assert not ui.interrupted_empty


async def test_resize_repaints_transcript_and_frame_preserving_input():
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class ResizableOutput(DummyOutput):
        columns = 100

        def get_size(self):
            return Size(rows=30, columns=self.columns)

    output = ResizableOutput()
    rendered = asyncio.Event()
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=output),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch.object(output, "erase_screen") as erase_screen,
        patch.dict("os.environ", {"TERM": "xterm-256color"}),
    ):
        ui = UI(color=False)
        capture = io.StringIO()
        ui.console = Console(file=capture, width=100, no_color=True)
        ui.say("Строка разговора " * 12)
        assert ui.prompt is not None
        app = ui.prompt.app
        app.after_render += lambda _: rendered.set()
        task = asyncio.create_task(ui.ask(chat=True))
        try:
            await asyncio.wait_for(rendered.wait(), 3)
            for width in (40, 120, 32):
                app.current_buffer.text = "неотправленный текст"
                capture.seek(0)
                capture.truncate()
                output.columns = width
                rendered.clear()
                app._on_resize()
                await asyncio.wait_for(rendered.wait(), 3)
                assert app.current_buffer.text == "неотправленный текст"
                assert "Строка разговора" in capture.getvalue()
                assert all(len(line) <= width for line in capture.getvalue().splitlines())
                screen = app.renderer.last_rendered_screen
                assert screen is not None
                lines = [
                    "".join(row[x].char for x in range(width))
                    for row in screen.data_buffer.values()
                ]
                assert sum(line == "─" * width for line in lines) == 2
            assert erase_screen.call_count == 3
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 3) == "неотправленный текст"
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


async def test_chat_frame_runs_with_terminal_layout():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
    ):
        ui = UI(color=False)
        assert ui.prompt is not None
        pipe.send_text("привет\r")
        assert await ui.ask(chat=True) == "привет"
        assert not ui.chat_input


async def test_command_menu_remains_visible_after_first_command():
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class CursorOutput(DummyOutput):
        rows_below_cursor = 40

        def get_rows_below_cursor_position(self):
            return self.rows_below_cursor

    output = CursorOutput()
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=output),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch.dict("os.environ", {"TERM": "xterm-256color"}),
    ):
        ui = UI(color=False)
        ui.console = Console(file=io.StringIO(), no_color=True)
        assert ui.prompt is not None
        rendered = asyncio.Event()
        app = ui.prompt.app
        app.after_render += lambda _: rendered.set()

        def screen_text():
            screen = app.renderer.last_rendered_screen
            assert screen is not None
            return "\n".join(
                "".join(cell.char for _, cell in sorted(row.items()))
                for _, row in sorted(screen.data_buffer.items())
            )

        pipe.send_text("/help\r")
        assert await ui.ask(chat=True) == "/help"
        # Simulate a command writing enough output to fill the terminal.
        ui.say("Справка\n" * 40)
        output.rows_below_cursor = 4
        task = asyncio.create_task(ui.ask(chat=True))
        try:
            pipe.send_text("/")
            async with asyncio.timeout(3):
                while not app.current_buffer.complete_state:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            rendered.clear()
            app.invalidate()
            await asyncio.wait_for(rendered.wait(), 3)
            assert "/model" in screen_text()
            assert "Поиск и смена модели" in screen_text()
            pipe.send_text("\x1b\x15/exit\r")
            assert await asyncio.wait_for(task, 3) == "/exit"
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.parametrize("newline", ["\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;2~"])
async def test_shift_enter_inserts_newline_and_enter_submits(newline):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.ui import input_bindings

    ui = UI(color=False)
    with create_pipe_input() as pipe:
        ui.prompt = PromptSession(
            input=pipe,
            output=DummyOutput(),
            key_bindings=input_bindings(ui),
            multiline=True,
        )
        pipe.send_text("первая" + newline + "вторая\r")
        assert await ui.ask(chat=True) == "первая\nвторая"


def test_reset_clears_terminal_and_scrollback(tmp_path):
    ui = UI(color=False)
    output = io.StringIO()
    ui.console = Console(file=output, force_terminal=True, no_color=True, force_interactive=True)
    ui.reset("test", tmp_path)
    assert "\x1b[2J" in output.getvalue()
    assert "\x1b[3J" in output.getvalue()
    assert "SpiritCode" in output.getvalue()
    assert ui.model_label == "test"


async def test_redirected_input_and_permission_refusal(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    with patch("sys.stdin", io.StringIO("привет\n")), patch("sys.stdout", io.StringIO()):
        ui = UI()
        assert await ui.ask() == "привет"
        assert not await ui.approve("echo danger")


def test_narrow_no_color_cyrillic_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    output = io.StringIO()
    ui = UI(color=False)
    ui.console = Console(file=output, width=28, no_color=True)
    ui.header("test", tmp_path)
    ui.begin_response()
    ui.delta("Привет\n```python\nprint('да')\n```")
    ui.end_response()
    text = output.getvalue()
    assert "SpiritCode" in text
    assert "Привет" in text
    assert "\x1b" not in text


async def test_password_never_enters_conversation_input_history():
    ui = UI()
    conversation_prompt = MagicMock()
    ui.prompt = conversation_prompt
    password_prompt = MagicMock()
    password_prompt.prompt_async = AsyncMock(return_value="private-key")
    with patch("spiritcode.ui.PromptSession", return_value=password_prompt) as factory:
        assert await ui.ask("Ключ: ", secret=True) == "private-key"
        assert isinstance(factory.call_args.kwargs["history"], DummyHistory)
    conversation_prompt.prompt_async.assert_not_called()


def test_slash_palette_descriptions_arguments_and_plain_prose(tmp_path, monkeypatch):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from spiritcode.commands import COMMANDS, help_text
    from spiritcode.ui import InputCompleter

    completer = InputCompleter()
    completer.models = ["provider/fast", "provider/deep"]
    completer.sessions = [("saved-session", "project · test model")]

    def completions(text):
        return list(completer.get_completions(Document(text), CompleteEvent()))

    entries = completions("/")
    assert [entry.text for entry in entries] == [command.name for command in COMMANDS]
    assert all(entry.display_meta_text for entry in entries)
    assert [entry.text for entry in completions("/con")] == ["/context"]
    assert [entry.text for entry in completions("/model DEEP")] == ["provider/deep"]
    assert [entry.text for entry in completions("/resume saved")] == ["saved-session"]
    assert [entry.text for entry in completions("/usage 3")] == ["30d"]
    assert not completions("Привет")
    assert not completions("/unknown ")
    assert not completions("/context")
    assert not completions("/usage 30d")
    assert all(command.name in help_text() for command in COMMANDS)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("test")
    assert completions("Посмотри @hel")[0].text == "lo.txt"
    (tmp_path / "sub folder").mkdir()
    (tmp_path / "sub folder" / "a.txt").write_text("test")
    assert completions('@"sub')[0].text == " folder/"
    assert completions('@"sub folder/a')[0].text == '.txt"'
    assert completions("@sub")[0].text == '"sub folder/'


async def test_palette_enter_accepts_without_submitting_and_escape_dismisses():
    import asyncio

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.ui import input_bindings

    ui = UI(color=False)
    with create_pipe_input() as pipe:
        ui.prompt = PromptSession(
            input=pipe,
            output=DummyOutput(),
            key_bindings=input_bindings(ui),
            multiline=True,
        )
        task = asyncio.create_task(ui.ask(chat=True))

        async def wait_for(predicate):
            async with asyncio.timeout(3):
                # The UI has no awaitable completion event; poll the real input buffer.
                while not predicate():  # noqa: ASYNC110
                    await asyncio.sleep(0.01)

        try:
            pipe.send_text("/con")
            buffer = ui.prompt.default_buffer
            await wait_for(lambda: buffer.complete_state is not None)
            pipe.send_text("\r")
            await wait_for(lambda: buffer.text == "/context" and not buffer.complete_state)
            assert not task.done()
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 3) == "/context"

            task = asyncio.create_task(ui.ask(chat=True))
            pipe.send_text("/mod")
            await wait_for(lambda: buffer.complete_state is not None)
            pipe.send_text("\x7f")
            await wait_for(lambda: buffer.text == "/mo" and buffer.complete_state is not None)
            pipe.send_text("\x15")
            await wait_for(lambda: buffer.text == "" and buffer.complete_state is not None)
            from spiritcode.commands import COMMANDS

            assert [entry.text for entry in buffer.complete_state.completions] == [
                command.name for command in COMMANDS
            ]
            pipe.send_text("/mod")
            await wait_for(lambda: buffer.text == "/mod" and buffer.complete_state is not None)
            pipe.send_text("\x1b")
            await wait_for(lambda: buffer.complete_state is None)
            assert buffer.text == "/mod"
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 3) == "/mod"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\x1b[B \r", ("unrestricted", False)),
        ("\x1b[A\r", ("full", True)),
        ("\x1b[B\x1b", None),
        ("\x03", None),
    ],
)
async def test_permission_menu_keyboard_and_cancel(keys, expected):
    import asyncio

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.config import Settings

    ui = UI(color=False)
    settings = Settings()
    with create_pipe_input() as pipe:
        ui.prompt = PromptSession(input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        async with asyncio.timeout(3):
            assert await ui.choose_permissions(settings) == expected
    assert settings.permission_mode == "standard"
    assert settings.confirm_deletion


async def test_permission_menu_without_terminal_preserves_settings():
    from spiritcode.config import Settings

    ui = UI(color=False)
    ui.prompt = None
    assert await ui.choose_permissions(Settings()) is None
