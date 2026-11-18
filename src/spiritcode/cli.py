import argparse
import asyncio
import os
import signal
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spiritcode import __version__
from spiritcode.commands.account import account, load_account_label
from spiritcode.commands.chat import Chat
from spiritcode.commands.diagnostics import show_context
from spiritcode.commands.files import show_diff, undo
from spiritcode.commands.model import (
    apply_model,
    choose_model,
    model_rows,
    resolve_model,
    set_model_label,
)
from spiritcode.commands.sessions import replace_session, show_sessions
from spiritcode.core.agent import Agent
from spiritcode.core.api import ApiError, Client
from spiritcode.core.config import Settings
from spiritcode.core.context import project_root
from spiritcode.core.credentials import Credentials
from spiritcode.core.store import Store, new_session
from spiritcode.tools.executor import Tools
from spiritcode.ui.commands import help_text
from spiritcode.ui.interface import UI


class RussianParser(argparse.ArgumentParser):
    def __init__(self, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(**kwargs)
        self._positionals.title = "Команды"
        self._optionals.title = "Параметры"
        self.add_argument("-h", "--help", action="help", help="Показать справку и выйти")


async def cancellable[T](operation: Coroutine[Any, Any, T], ui: UI) -> T | None:
    task = asyncio.create_task(operation)
    loop = asyncio.get_running_loop()
    # signal.signal works on Windows as well; event-loop signal handlers do not.
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(task.cancel))
    try:
        return await task
    except asyncio.CancelledError:
        ui.notice("Операция прервана")
        return None
    finally:
        signal.signal(signal.SIGINT, previous)


async def interactive(
    client: Client,
    settings: Settings,
    ui: UI,
    store: Store,
    session: dict[str, Any] | None = None,
    *,
    header_shown: bool = False,
) -> None:
    root = Path(session["root"]) if session else project_root(Path.cwd())
    if not root.is_dir():
        raise ValueError("Каталог сохранённого проекта больше не существует.")

    requested = session["model"] if session else settings.model
    if not header_shown:
        ui.header(requested, root)
    if settings.permissions_warning:
        ui.notice("Настройки разрешений сброшены", "Включена стандартная защита.", "warning")

    model = await resolve_model(client, ui, requested)
    set_model_label(ui, model)

    session = session or new_session(root, model.id if model else "")
    tools = Tools(root, ui, session["snapshots"], settings)
    chat = Chat(
        client=client,
        settings=settings,
        ui=ui,
        store=store,
        root=root,
        session=session,
        model=model,
        tools=tools,
        agent=Agent(client, model, session, store, tools, ui) if model else None,
    )
    store.save(session)
    os.chdir(root)

    if chat.model is None:
        ui.muted("Выберите /model, чтобы отправить первое сообщение.")

    interrupted = False
    while True:
        ui.completer.sessions = [
            (row[0], f"{row[1] or 'без модели'} · {row[2]}") for row in store.list()
        ]

        try:
            text = (await ui.ask(chat=True)).strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            if not ui.interrupted_empty:
                interrupted = False
                ui.muted("Ввод очищен.")
                continue
            if interrupted:
                break
            interrupted = True
            ui.muted("Нажмите Ctrl+C ещё раз для выхода.")
            continue

        interrupted = False
        if not text:
            continue

        try:
            if not text.startswith("/"):
                if chat.agent is None:
                    ui.notice(
                        "Модель не выбрана",
                        "Используйте /model, затем отправьте сообщение.",
                        "warning",
                    )
                    continue
                await cancellable(chat.agent.turn(text), ui)
                continue

            command, *parts = text.split(maxsplit=1)
            argument = parts[0].strip() if parts else ""
            client.request_id = None

            if command == "/exit":
                break

            if command == "/help":
                ui.say(help_text())

            elif command == "/permissions":
                choice = await ui.choose_permissions(settings)
                if choice is not None:
                    settings.save_permissions(*choice)
                    ui.notice("Разрешения сохранены", "Настройки действуют для всех проектов.")

            elif command == "/context":
                await cancellable(show_context(chat.session, chat.agent, ui), ui)

            elif command == "/model":
                chosen = await cancellable(choose_model(client, settings, ui, argument or None), ui)
                if chosen is not None:
                    await apply_model(chat, chosen)

            elif command in {"/profile", "/balance", "/subscription", "/usage"}:
                await cancellable(account(client, ui, command[1:], argument or "2d"), ui)

            elif command == "/sessions":
                show_sessions(store, ui)

            elif command in {"/new", "/resume"}:
                await replace_session(chat, command, argument)

            elif command == "/compact":
                if chat.agent:
                    await cancellable(chat.agent.compact(), ui)
                else:
                    ui.notice(
                        "Модель не выбрана", "Используйте /model для сжатия контекста.", "warning"
                    )

            elif command == "/undo":
                await undo(chat)

            elif command == "/diff":
                show_diff(chat)

            else:
                ui.notice(
                    "Неизвестная команда",
                    "Введите / для выбора команды или /help для справки.",
                    "warning",
                )

        except (ApiError, ValueError, OSError) as exc:
            if isinstance(exc, ApiError) and text.startswith("/") and command != "/compact":
                chat.session.setdefault("diagnostics", []).append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "kind": command,
                        "status": "error",
                        "request_id": exc.request_id or client.request_id,
                        "usage": None,
                    }
                )
                store.save(chat.session)
            ui.notice("Ошибка", str(exc), "error")
        except EOFError:
            break
        except KeyboardInterrupt:
            ui.notice("Операция прервана")


async def run(args: argparse.Namespace) -> None:
    settings = Settings.load(args.base_url, args.model)
    ui = UI(settings.color)
    credentials = Credentials(settings.base_url)
    store = Store()
    client: Client | None = None

    try:
        session = store.load(args.id) if args.command == "resume" else None
        if session and (args.model or os.getenv("SPIRITLM_MODEL")):
            session["model"] = args.model or os.environ["SPIRITLM_MODEL"]

        chat = args.command in {None, "resume"}
        if chat:
            root = Path(session["root"]) if session else project_root(Path.cwd())
            ui.header(session["model"] if session else settings.model, root)

        if args.command == "sessions":
            show_sessions(store, ui)
            return

        if args.command == "logout":
            ui.say("Ключ удалён." if credentials.delete() else "Системное хранилище недоступно.")
            if os.getenv("SPIRITLM_API_KEY"):
                ui.say("Ключ окружения активен до удаления SPIRITLM_API_KEY из оболочки.")
            return

        key = credentials.get()
        if args.command == "login" or not key:
            key = (await ui.ask("API-ключ SpiritLM: ", secret=True)).strip()
            if not key:
                raise ValueError("API-ключ не указан.")

            client = Client(settings, key)
            await client.get("models")

            if credentials.save(key):
                ui.notice("Вход выполнен", "API-ключ сохранён в системном хранилище.", "success")
            else:
                ui.notice(
                    "Keyring недоступен", "Ключ хранится только в памяти этого процесса.", "warning"
                )
                if args.command == "login":
                    ui.say("Для следующего запуска задайте SPIRITLM_API_KEY или настройте keyring.")
            if args.command == "login":
                return

        client = client or Client(settings, key)

        if args.command == "models":
            models = await client.models()
            ui.table(model_rows(models), "Модели SpiritLM")

        elif args.command == "account":
            for name in ("profile", "balance", "subscription", "usage"):
                try:
                    await account(client, ui, name)
                except ApiError as exc:
                    ui.notice("Ошибка", str(exc), "error")

        else:
            await load_account_label(client, ui)
            root = Path(session["root"]) if session else project_root(Path.cwd())
            ui.reset(session["model"] if session else settings.model, root)
            await interactive(client, settings, ui, store, session, header_shown=True)
    finally:
        if client:
            await client.close()
        store.close()


def main() -> None:
    parser = RussianParser(prog="sprt", description="SpiritCode — кодинг-агент для SpiritLM")
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="Показать версию и выйти",
    )
    parser.add_argument("--base-url", help="Адрес SpiritLM без /v1")
    parser.add_argument("--model", help="ID модели SpiritLM")

    commands = parser.add_subparsers(dest="command")
    for command, description in {
        "login": "Войти",
        "logout": "Удалить ключ",
        "models": "Каталог моделей",
        "account": "Аккаунт SpiritLM",
        "sessions": "Сохранённые сессии",
    }.items():
        commands.add_parser(command, help=description)
    resume = commands.add_parser("resume", help="Продолжить сессию")
    resume.add_argument("id")

    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (EOFError, KeyboardInterrupt):
        pass
    except (ApiError, ValueError, OSError) as exc:
        UI().notice("Ошибка", str(exc), "error")
        raise SystemExit(1) from None
