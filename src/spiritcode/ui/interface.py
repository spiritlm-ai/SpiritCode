import asyncio
import getpass
import io
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, in_terminal, run_in_terminal
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion, DummyCompleter, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, is_done
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from spiritcode.constants import COLORS, STAR
from spiritcode.core.config import Settings
from spiritcode.sanitize import clean
from spiritcode.types import PERMISSION_MODES, NoticeKind, PermissionMode
from spiritcode.ui.commands import COMMANDS
from spiritcode.ui.keyboard import prepare_keyboard


class InputCompleter(Completer):
    def __init__(self) -> None:
        self.models: list[str] = []
        self.sessions: list[tuple[str, str]] = []

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        text = document.text_before_cursor
        if document.is_cursor_at_the_end and re.fullmatch(r"/[^\s/]*(?: [^\s]*)?", document.text):
            command, separator, argument = text.partition(" ")
            if not separator:
                for item in COMMANDS:
                    if item.name.startswith(command) and item.name != command:
                        yield Completion(
                            item.name,
                            start_position=-len(command),
                            display_meta=item.description,
                        )
                return

            candidates: list[tuple[str, str]] = []
            if command == "/model":
                candidates = [(model, "Модель SpiritLM") for model in self.models]
            elif command == "/resume":
                candidates = self.sessions
            elif command == "/usage":
                candidates = [
                    (period, "Период статистики") for period in ("2d", "7d", "30d", "all")
                ]
            for value, description in candidates:
                if argument.casefold() in value.casefold() and argument != value:
                    yield Completion(
                        value,
                        start_position=-len(argument),
                        display_meta=clean(description),
                        display=clean(value),
                    )
            return

        # Completion is opt-in for normal text; never pop a file menu over prose.
        match = re.search(r'(?:^|\s)@("?)([^"\n]*)$', text)
        if match:
            quote, path = match.groups()
            for completion in PathCompleter(expanduser=True).get_completions(
                Document(path), complete_event
            ):
                directory = completion.display_text.endswith("/")
                if directory:
                    completion.text += "/"

                if quote and not directory:
                    completion.text += '"'
                elif not quote and " " in path + completion.text:
                    completion = Completion(
                        '"' + path + completion.text + ("" if directory else '"'),
                        start_position=-len(path),
                        display=completion.display,
                    )
                yield completion


def input_bindings(ui: "UI") -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: Any) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and state.completions:
            completion = state.current_completion or state.completions[0]
            buffer.cancel_completion()
            buffer.apply_completion(completion)
        else:
            buffer.validate_and_handle()

    @bindings.add("tab")
    def complete(event: Any) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("escape", filter=has_completions)
    def close_menu(event: Any) -> None:
        event.current_buffer.cancel_completion()

    @bindings.add("escape", "enter")
    @bindings.add("c-o")
    def newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c")
    def interrupt(event: Any) -> None:
        ui.interrupted_empty = not event.current_buffer.text
        if ui.chat_requests is not None:
            if not ui.interrupted_empty:
                event.current_buffer.reset()
            elif ui.cancel_active is not None:
                ui.cancel_active()
            else:
                ui.chat_requests.put_nowait(KeyboardInterrupt())
            return
        event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("insert")
    def commands(event: Any) -> None:
        event.current_buffer.insert_text("/")

    return bindings


class UI:
    def __init__(self, color: bool = True) -> None:
        self.interrupted_empty = True
        self.color = color and "NO_COLOR" not in os.environ
        self.console = Console(no_color=not self.color, highlight=False)
        self.completer = InputCompleter()
        self.model_label = "модель не выбрана"
        self.account_label = "Аккаунт не загружен"
        self.chat_input = False
        self.transcript: list[tuple[object, ...]] = []
        self.terminal_size: tuple[int, int] | None = None
        self.prompt: PromptSession[str] | None = None
        self.live: Live | None = None
        self.response = ""
        self.streaming = False
        self.chat_requests: asyncio.Queue[str | BaseException] | None = None
        self.cancel_active: Callable[[], object] | None = None
        self.terminal_writes: list[Callable[[], None]] = []
        self.terminal_job: asyncio.Future[None] | None = None
        self.style = self._build_style()

        if sys.stdin.isatty() and sys.stdout.isatty():
            self.prompt = self._build_prompt()

    def _build_style(self) -> Style:
        if not self.color:
            return Style.from_dict({})
        return Style.from_dict(
            {
                "prompt": f"bold {COLORS['accent']}",
                "hint": COLORS["muted"],
                "input-border": "#515967",
                "completion-menu.completion": "bg:#1b1b1b #f3f2ee",
                "completion-menu.completion.current": "bg:#ffe02e #080808",
                "completion-menu.meta.completion": "bg:#1b1b1b #a5a5a1",
                "completion-menu.meta.completion.current": "bg:#30302e #f3f2ee",
                "bottom-toolbar": "noreverse",
            }
        )

    def _build_prompt(self) -> PromptSession[str]:
        prompt: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=input_bindings(self),
            completer=self.completer,
            complete_while_typing=True,
            enable_history_search=False,
            style=self.style,
            color_depth=None if self.color else ColorDepth.DEPTH_1_BIT,
            reserve_space_for_menu=0,
            prompt_continuation="  ",
            erase_when_done=True,
        )
        prompt.app.before_render += self._before_render
        self._frame_layout(prompt)
        return prompt

    def _frame_layout(self, prompt: PromptSession[str]) -> None:
        layout = prompt.layout.container
        assert isinstance(layout, HSplit)
        chat_active = Condition(lambda: self.chat_input) & ~is_done

        def separator() -> ConditionalContainer:
            return ConditionalContainer(
                Window(height=1, char="─", style="class:input-border"),
                filter=chat_active,
            )

        layout.children.insert(0, separator())
        layout.children.insert(2, separator())
        layout.children.insert(
            3,
            ConditionalContainer(
                VSplit(
                    [
                        Window(
                            FormattedTextControl(self._chat_hint),
                            width=18,
                            height=1,
                            style="class:hint",
                        ),
                        Window(
                            FormattedTextControl(lambda: self.model_label + " "),
                            align=WindowAlign.RIGHT,
                            height=1,
                            wrap_lines=False,
                            style="class:hint",
                        ),
                    ]
                ),
                filter=chat_active,
            ),
        )
        layout.children.insert(
            0,
            ConditionalContainer(
                Window(FormattedTextControl(self._stream_preview)),
                filter=Condition(lambda: self.streaming and self.chat_requests is not None),
            ),
        )

    def _print(self, *objects: object) -> None:
        def write() -> None:
            self.transcript.append(objects)
            self.console.print(*objects)

        self._terminal_write(write)

    def _chat_hint(self) -> str:
        waiting = self.chat_requests.qsize() if self.chat_requests is not None else 0
        return f" В очереди: {waiting}" if waiting else " /help — команды"

    def _terminal_write(self, write: Callable[[], None]) -> None:
        if self.chat_requests is None or self.prompt is None or not self.prompt.app.is_running:
            write()
            return
        self.terminal_writes.append(write)
        if self.terminal_job is None or self.terminal_job.done():

            def flush() -> None:
                writes, self.terminal_writes = self.terminal_writes, []
                for item in writes:
                    item()

            with set_app(self.prompt.app):
                self.terminal_job = asyncio.ensure_future(run_in_terminal(flush))

    def _stream_preview(self) -> ANSI:
        assert self.prompt is not None
        size = self.prompt.app.output.get_size()
        stream = io.StringIO()
        console = Console(
            file=stream,
            width=max(1, size.columns),
            force_terminal=self.color,
            no_color=not self.color,
        )
        console.print(self._response_content() if self.response else Text("  Думаю…"))
        return ANSI("\n".join(stream.getvalue().splitlines()[-max(1, size.rows // 2) :]))

    @asynccontextmanager
    async def chat_session(self) -> AsyncIterator[None]:
        if self.prompt is None:
            yield
            return

        queue: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.chat_requests = queue
        buffer = self.prompt.default_buffer
        previous_handler = buffer.accept_handler

        def accept(buffer: Buffer) -> bool:
            if buffer.text.strip():
                self._print(Text("❯ " + clean(buffer.text), style=COLORS["accent"]))
                queue.put_nowait(buffer.text)
            return False

        async def read_input() -> None:
            try:
                await self.ask(chat=True)
            except (Exception, KeyboardInterrupt) as exc:
                queue.put_nowait(exc)

        buffer.accept_handler = accept
        reader = asyncio.create_task(read_input())
        try:
            yield
        finally:
            try:
                if self.terminal_job is not None:
                    await self.terminal_job
            finally:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader
                buffer.accept_handler = previous_handler
                self.chat_requests = None

    async def next_chat_input(self) -> str:
        if self.chat_requests is None:
            return await self.ask(chat=True)
        value = await self.chat_requests.get()
        if self.prompt is not None:
            self.prompt.app.invalidate()
        if isinstance(value, BaseException):
            raise value
        return value

    @asynccontextmanager
    async def _modal(self) -> AsyncIterator[None]:
        if self.chat_requests is None or self.prompt is None:
            yield
            return
        if self.terminal_job is not None:
            await self.terminal_job
        with set_app(self.prompt.app):
            async with in_terminal():
                yield

    def _before_render(self, app: Application[str]) -> None:
        # Grow the input area only while a menu is open. Otherwise a prompt
        # near the terminal bottom leaves no room for the floating completions.
        state = app.current_buffer.complete_state
        if self.prompt:
            self.prompt.reserve_space_for_menu = (
                min(len(state.completions), 6) + 1 if state and state.completions else 0
            )

        size = app.output.get_size()
        width = size.columns
        previous_size = self.terminal_size
        self.terminal_size = (size.columns, size.rows)
        if previous_size is None or previous_size == self.terminal_size:
            return

        # Re-render Rich objects at the new width. Old terminal cells cannot be
        # reflowed reliably: full-width rules otherwise leave duplicate rows.
        app.output.erase_screen()
        app.output.cursor_goto(0, 0)
        app.output.flush()
        for objects in self.transcript:
            self.console.print(*objects, width=width)
        app.renderer.reset()
        app.renderer.request_absolute_cursor_position()

    def say(self, text: str) -> None:
        self._print(Text(clean(text), style=COLORS["text"]))

    def muted(self, text: str) -> None:
        self._print(Text(clean(text), style=COLORS["muted"]))

    def notice(self, title: str, body: str = "", kind: NoticeKind = "info") -> None:
        color = COLORS["accent" if kind == "warning" else kind]
        symbol = {"info": "│", "warning": "!", "error": "×", "success": "✓"}[kind]

        self._print()
        self._print(Text(f"{symbol} {clean(title)}", style=f"bold {color}"))
        if body:
            self._print(Padding(Text(clean(body), style=COLORS["muted"]), (0, 0, 0, 2)))
        self._print()

    def header(self, model: str | None, root: Path) -> None:
        self.model_label = clean(model) if model else "модель не выбрана · /model"

        info = Text()
        info.append("SpiritCode\n", style=f"bold {COLORS['accent']}")
        info.append(clean(self.account_label) + "\n", style=COLORS["muted"])
        info.append(self.model_label + "\n", style=COLORS["muted"])
        info.append(clean(str(root)), style=COLORS["muted"])

        self._print()
        if self.console.width >= 64:
            layout = Table.grid(padding=(0, 3))
            layout.add_column(no_wrap=True)
            layout.add_column(ratio=1)
            layout.add_row(Text(STAR, style=COLORS["accent"]), Padding(info, (2, 0)))
            self._print(layout)
        else:
            self._print(info)
        self._print()

    def reset(self, model: str | None, root: Path) -> None:
        def clear() -> None:
            self.transcript.clear()
            if self.console.is_terminal:
                self.console.file.write("\x1b[2J\x1b[H\x1b[3J")
                self.console.file.flush()

        self._terminal_write(clear)
        self.header(model, root)

    async def ask(
        self,
        text: str = "❯ ",
        *,
        secret: bool = False,
        completer: Completer | None = None,
        chat: bool = False,
    ) -> str:
        if self.prompt:
            if not chat and self.chat_requests is not None:
                async with self._modal():
                    selection: PromptSession[str] = PromptSession(
                        input=self.prompt.app.input,
                        output=self.prompt.app.output,
                        style=self.style,
                        completer=completer,
                        history=DummyHistory(),
                    )
                    return await selection.prompt_async(clean(text), is_password=secret)
            if secret:
                password_prompt: PromptSession[str] = PromptSession(
                    history=DummyHistory(),
                    style=self.style,
                    color_depth=None if self.color else ColorDepth.DEPTH_1_BIT,
                )
                return await password_prompt.prompt_async(clean(text), is_password=True)

            self.chat_input = chat
            buffer = self.prompt.default_buffer
            previous_text = ""

            def complete_after_deletion(changed_buffer: Buffer) -> None:
                nonlocal previous_text
                deleted = len(changed_buffer.text) < len(previous_text)
                previous_text = changed_buffer.text
                if deleted and (chat or completer is not None):
                    changed_buffer.start_completion()

            # prompt-toolkit only autocompletes insertions by default.
            buffer.on_text_changed += complete_after_deletion

            extended_keyboard = prepare_keyboard(self.prompt.app.input) and chat
            output = self.prompt.app.output
            if extended_keyboard:
                output.write_raw("\x1b[>5u")
                output.flush()

            try:
                result = await self.prompt.prompt_async(
                    [("class:prompt", clean(text))],
                    completer=(completer or self.completer)
                    if chat or completer
                    else DummyCompleter(),
                    complete_while_typing=chat or completer is not None,
                )
                if chat and result.strip():
                    self._print(Text("❯ " + clean(result), style=COLORS["accent"]))
                return result
            finally:
                buffer.on_text_changed -= complete_after_deletion
                if extended_keyboard:
                    output.write_raw("\x1b[<u")
                    output.flush()
                self.chat_input = False

        # Piped input has no cursor controls, animations, or background reader.
        if secret:
            return getpass.getpass(clean(text))

        line = sys.stdin.readline()
        if not line:
            raise EOFError
        value = line.rstrip("\n")
        if chat and value.strip():
            self._print(Text("❯ " + clean(value), style=COLORS["accent"]))
        return value

    async def choose_permissions(self, settings: Settings) -> tuple[PermissionMode, bool] | None:
        if self.prompt is None:
            self.notice("Настройки не изменены", "Нужен интерактивный терминал.", "warning")
            return None

        labels = ("Полная защита", "Стандартная защита", "Без защиты")
        details = (
            "Каждая shell-команда требует разрешения. Точечные правки автоматические.",
            "Git, удаление, перезапись и непрозрачные команды требуют разрешения.",
            "Команды выполняются автоматически с учётом чекбокса удаления.",
        )
        selected = PERMISSION_MODES.index(settings.permission_mode)
        deletion = settings.confirm_deletion
        keys = KeyBindings()

        @keys.add("up")
        def previous(event: Any) -> None:
            nonlocal selected
            selected = (selected - 1) % len(labels)

        @keys.add("down")
        def following(event: Any) -> None:
            nonlocal selected
            selected = (selected + 1) % len(labels)

        @keys.add(" ")
        def toggle(event: Any) -> None:
            nonlocal deletion
            if PERMISSION_MODES[selected] == "unrestricted":
                deletion = not deletion

        @keys.add("enter")
        def accept(event: Any) -> None:
            event.app.exit(result=(PERMISSION_MODES[selected], deletion))

        @keys.add("escape")
        @keys.add("c-c")
        @keys.add("c-d")
        def cancel(event: Any) -> None:
            event.app.exit(result=None)

        def content() -> StyleAndTextTuples:
            rows: StyleAndTextTuples = [("", "Разрешения · для всех проектов\n\n")]
            for index, label in enumerate(labels):
                rows.append(
                    (
                        "class:prompt" if index == selected else "",
                        ("❯ " if index == selected else "  ") + label + "\n",
                    )
                )

            rows.append(("", "\n" + details[selected] + "\n"))
            if PERMISSION_MODES[selected] == "unrestricted":
                rows.append(("", ("[x]" if deletion else "[ ]") + " Спрашивать перед удалением\n"))

            rows.append(
                (
                    "class:hint",
                    "\n↑/↓ — режим · Space — чекбокс · Enter — сохранить · Esc — назад\n"
                    "Подтверждения не являются системной песочницей.\n",
                )
            )
            return rows

        app: Application[tuple[PermissionMode, bool] | None] = Application(
            layout=Layout(Window(FormattedTextControl(content))),
            key_bindings=keys,
            style=self.style,
            input=self.prompt.app.input,
            output=self.prompt.app.output,
            full_screen=False,
            erase_when_done=True,
        )
        try:
            async with self._modal():
                return await app.run_async()
        except (EOFError, KeyboardInterrupt):
            return None

    async def approve(self, description: str) -> bool:
        self.notice("Ожидание разрешения", description, "warning")
        if not self.prompt:
            self.notice("Действие отклонено", "Нет интерактивного терминала.", "warning")
            return False

        try:
            return await self._approve_buttons()
        except (KeyboardInterrupt, EOFError):
            return False

    async def _approve_buttons(self) -> bool:
        assert self.prompt is not None
        selected = False
        keys = KeyBindings()

        @keys.add("left")
        @keys.add("right")
        @keys.add("tab")
        @keys.add("s-tab")
        def toggle(event: Any) -> None:
            nonlocal selected
            selected = not selected

        @keys.add("enter")
        @keys.add(" ")
        def accept(event: Any) -> None:
            event.app.exit(result=selected)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-d")
        def reject(event: Any) -> None:
            event.app.exit(result=False)

        def click_yes(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                app.exit(result=True)

        def click_no(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                app.exit(result=False)

        def buttons() -> StyleAndTextTuples:
            return [
                ("", "Разрешить действие?\n"),
                (
                    "class:prompt" if selected else "",
                    "❯ [ Да ]" if selected else "  [ Да ]",
                    click_yes,
                ),
                (
                    "class:prompt" if not selected else "",
                    "   [ Нет ]" if selected else " ❯ [ Нет ]",
                    click_no,
                ),
                ("class:hint", "\n←/→ или Tab — выбор · Enter — подтвердить · Esc — отказ"),
            ]

        app: Application[bool] = Application(
            layout=Layout(Window(FormattedTextControl(buttons))),
            key_bindings=keys,
            style=self.style,
            input=self.prompt.app.input,
            output=self.prompt.app.output,
            erase_when_done=True,
            mouse_support=True,
        )
        async with self._modal():
            return await app.run_async()

    def begin_response(self) -> None:
        self.response = ""
        self._print()
        self._print(Text("● SpiritCode", style=f"bold {COLORS['accent']}"))
        if self.chat_requests is not None and self.prompt is not None:
            self.streaming = True
            self.prompt.app.invalidate()
        elif self.console.is_terminal:
            self.live = Live(
                Padding(Spinner("dots", text="Думаю…", style=COLORS["muted"]), (0, 0, 0, 2)),
                console=self.console,
                refresh_per_second=8,
            )
            self.live.start()

    def delta(self, text: str) -> None:
        self.response += text
        if self.live:
            self.live.update(self._response_content())
        elif self.streaming and self.prompt is not None:
            self.prompt.app.invalidate()

    def _response_content(self) -> Padding:
        return Padding(Markdown(clean(self.response)), (0, 0, 0, 2))

    def end_response(self) -> None:
        content = self._response_content()
        self.streaming = False
        if self.live:
            self.live.update(content)
            self.live.stop()
            self.live = None
            self.transcript.append((content,))
        elif self.response:
            self._print(content)
        self._print()

    def tool(self, title: str, detail: str, *, error: bool = False) -> None:
        text = Text(
            "  × " if error else "  · ", style=COLORS["error"] if error else COLORS["muted"]
        )
        text.append(clean(title), style=COLORS["error"] if error else COLORS["info"])
        text.append("  " + clean(detail), style=COLORS["muted"])
        self._print(text)

    def diff(self, text: str) -> None:
        self._print(Padding(Syntax(clean(text), "diff", word_wrap=True), (0, 0, 0, 2)))

    def table(self, rows: list[tuple[str, str]], title: str = "") -> None:
        table = Table(
            title=Text(clean(title), style=f"bold {COLORS['accent']}"),
            title_justify="left",
            show_header=False,
            box=box.SIMPLE,
            padding=(0, 1),
            expand=False,
        )
        table.add_column(style=COLORS["muted"])
        table.add_column(style=COLORS["text"])
        for key, value in rows:
            table.add_row(Text(clean(key)), Text(clean(value)))
        self._print(table)
