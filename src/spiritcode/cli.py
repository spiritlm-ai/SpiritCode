import argparse
import asyncio
import os
import signal
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import FuzzyWordCompleter

from spiritcode import __version__
from spiritcode.agent import Agent
from spiritcode.api import ApiError, Client, Model
from spiritcode.commands import help_text
from spiritcode.config import ACCOUNT_TIMEOUT, DEFAULT_OUTPUT_TOKENS, Settings
from spiritcode.context import estimate_tokens, input_budget, project_root, system_context
from spiritcode.credentials import Credentials
from spiritcode.display import account_rows
from spiritcode.store import Store, new_session
from spiritcode.tools import Tools, unified
from spiritcode.ui import UI, clean

PERMISSIONS = """Чтение и правки в проекте разрешены автоматически.
Каждая команда и каждый доступ вне проекта требуют отдельного разрешения.
Секреты читаются только после разрешения и не редактируются инструментами.
Симлинки проверяются по фактическому пути. Команды имеют права пользователя:
подтверждение не является системной песочницей. /undo не отменяет команды."""


class RussianParser(argparse.ArgumentParser):
    def __init__(self, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(**kwargs)
        self._positionals.title = "Команды"
        self._optionals.title = "Параметры"
        self.add_argument("-h", "--help", action="help", help="Показать справку и выйти")


def model_rows(models: list[Model]) -> list[tuple[str, str]]:
    return [
        (
            model.id,
            (
                f"{Decimal(model.price_per_million_tokens):.2f}"
                if model.price_per_million_tokens is not None
                else "неизвестно"
            )
            + " RUB / 1 MTok",
        )
        for model in models
    ]


def set_model_label(ui: UI, model: Model | None) -> None:
    ui.model_label = clean(model.id) if model else "модель не выбрана · /model"


async def resolve_model(client: Client, ui: UI, requested: str | None) -> Model | None:
    if not requested:
        return None
    try:
        models = await client.models()
    except ApiError as exc:
        ui.notice(
            "Не удалось загрузить модель", str(exc) + " Выберите модель через /model.", "warning"
        )
        return None
    ui.completer.models = [model.id for model in models]
    selected = next((model for model in models if model.id == requested), None)
    if selected is None:
        ui.notice("Модель недоступна", "Выберите доступную модель через /model.", "warning")
    elif selected.tool_calling is not True:
        ui.muted("Режим чата · поддержка инструментов не подтверждена сервером.")
    set_model_label(ui, selected)
    return selected


async def choose_model(
    client: Client, settings: Settings, ui: UI, requested: str | None = None
) -> Model | None:
    models = await client.models()
    ui.completer.models = [model.id for model in models]
    if not models:
        raise ApiError("В каталоге SpiritLM нет доступных моделей.")
    if requested is None:
        ui.table(model_rows(models), "Модели SpiritLM")
        requested = (
            await ui.ask(
                "Модель (пустая строка — назад): ",
                completer=FuzzyWordCompleter([model.id for model in models]),
            )
        ).strip()
        if not requested:
            return None
    selected = next((model for model in models if model.id == requested), None)
    if selected is None:
        raise ValueError("Выберите точный ID модели из /model.")
    settings.save_model(selected.id)
    set_model_label(ui, selected)
    return selected


async def show_context(session: dict[str, Any], agent: Agent | None, ui: UI) -> None:
    rows = [("Сессия", session["id"]), ("Модель", agent.model.id if agent else "не выбрана")]
    if agent:
        context = {
            "role": "system",
            "content": await asyncio.to_thread(system_context, agent.tools.root),
        }
        output = min(DEFAULT_OUTPUT_TOKENS, agent.model.max_output_tokens or DEFAULT_OUTPUT_TOKENS)
        budget = input_budget(agent.model.context_window, output)
        estimated = estimate_tokens([context, *agent.messages]) + estimate_tokens(
            agent.payload([]).get("tools", [])
        )
        rows.extend(
            [
                (
                    "Контекст (оценка)",
                    f"≈ {estimated} / {budget} токенов · {estimated / budget:.0%}",
                ),
                (
                    "Окно модели",
                    str(agent.model.context_window)
                    if agent.model.context_window
                    else "неизвестно · бюджет рассчитан из резервного окна",
                ),
                ("Резерв ответа", f"{output} токенов"),
            ]
        )
    else:
        rows.append(("Контекст", "Выберите /model для оценки бюджета"))
    diagnostics = session.get("diagnostics", [])
    rows.append(("Запросов в сессии", str(len(diagnostics))))
    if diagnostics:
        latest = diagnostics[-1]
        statuses = {"completed": "завершён", "error": "ошибка", "cancelled": "прерван"}
        kinds = {"generation": "генерация", "compaction": "сжатие"}
        usage = latest.get("usage") or {}
        rows.extend(
            [
                ("Последний запрос", kinds.get(latest["kind"], latest["kind"])),
                ("Статус", statuses.get(latest["status"], latest["status"])),
                ("Время UTC", latest["at"]),
                ("Модель запроса", latest.get("model") or "нет данных"),
                ("Request ID", latest.get("request_id") or "нет данных"),
            ]
        )
        for key, label in (
            ("prompt_tokens", "Входные токены (сервер)"),
            ("completion_tokens", "Выходные токены (сервер)"),
            ("total_tokens", "Всего токенов (сервер)"),
        ):
            value = usage.get(key)
            rows.append((label, str(value) if value is not None else "нет данных"))
    else:
        rows.extend([("Request ID", "нет данных"), ("Токены (сервер)", "нет данных")])
    ui.table(rows, "Контекст и диагностика")


async def account(client: Client, ui: UI, name: str = "profile", period: str = "2d") -> None:
    if name == "usage" and period not in {"2d", "7d", "30d", "all"}:
        raise ValueError("Период: 2d, 7d, 30d или all.")
    value = await client.get(name, **({"period": period} if name == "usage" else {}))
    ui.table(account_rows(value), "Аккаунт SpiritLM")


async def load_account_label(client: Client, ui: UI) -> None:
    async def optional_account(name: str) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout(ACCOUNT_TIMEOUT):
                value = await client.get(name)
                return value if isinstance(value, dict) else None
        except (ApiError, TimeoutError):
            return None

    profile, subscription = await asyncio.gather(
        optional_account("profile"), optional_account("subscription")
    )
    identity = "Пользователь"
    if profile:
        username = profile.get("username")
        name = profile.get("name")
        if isinstance(username, str) and username.strip():
            identity = "@" + username.strip().lstrip("@")
        elif isinstance(name, str) and name.strip():
            identity = name.strip()
    plan = "тариф недоступен"
    if subscription is not None and "current" in subscription:
        current = subscription["current"]
        if current is None:
            plan = "Free"
        elif isinstance(current, dict):
            plan_name = current.get("planName")
            if isinstance(plan_name, str) and plan_name.strip():
                plan = plan_name.strip()
    ui.account_label = clean(f"{identity} | {plan}")


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
    model = await resolve_model(client, ui, requested)
    set_model_label(ui, model)
    session = session or new_session(root, model.id if model else "")
    tools = Tools(root, ui, session["snapshots"])
    agent = Agent(client, model, session, store, tools, ui) if model else None
    store.save(session)
    os.chdir(root)
    if model is None:
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
                if agent is None:
                    ui.notice(
                        "Модель не выбрана",
                        "Используйте /model, затем отправьте сообщение.",
                        "warning",
                    )
                    continue
                await cancellable(agent.turn(text), ui)
                continue
            command, *parts = text.split(maxsplit=1)
            argument = parts[0].strip() if parts else ""
            client.request_id = None
            if command == "/exit":
                break
            if command == "/help":
                ui.say(help_text())
            elif command == "/permissions":
                ui.notice("Разрешения", PERMISSIONS)
            elif command == "/context":
                await cancellable(show_context(session, agent, ui), ui)
            elif command == "/model":
                chosen = await cancellable(choose_model(client, settings, ui, argument or None), ui)
                if chosen is None:
                    continue
                model = chosen
                session["model"] = model.id
                if agent:
                    agent.model = model
                    agent.save()
                else:
                    agent = Agent(client, model, session, store, tools, ui)
                store.save(session)
                ui.reset(model.id, root)
            elif command in {"/profile", "/balance", "/subscription", "/usage"}:
                await cancellable(account(client, ui, command[1:], argument or "2d"), ui)
            elif command == "/sessions":
                show_sessions(store, ui)
            elif command in {"/new", "/resume"}:
                replacement = (
                    store.load(argument)
                    if command == "/resume"
                    else new_session(root, model.id if model else "")
                )
                replacement_root = await asyncio.to_thread(Path(replacement["root"]).resolve)
                if not replacement_root.is_dir():
                    raise ValueError("Каталог сессии не существует.")
                if command == "/resume":
                    model = await resolve_model(client, ui, replacement["model"])
                root, session = replacement_root, replacement
                tools = Tools(root, ui, session["snapshots"])
                agent = Agent(client, model, session, store, tools, ui) if model else None
                store.save(session)
                os.chdir(root)
                ui.reset(model.id if model else None, root)
            elif command == "/compact":
                if agent:
                    await cancellable(agent.compact(), ui)
                else:
                    ui.notice(
                        "Модель не выбрана", "Используйте /model для сжатия контекста.", "warning"
                    )
            elif command == "/undo":
                result = await tools.undo()
                session["messages"].append(
                    {"role": "user", "content": "Результат /undo: " + result}
                )
                store.save(session)
                ui.notice("Отмена правок", result)
            elif command == "/diff":
                if not tools.snapshots:
                    ui.muted("В этой сессии пока нет правок.")
                for group in tools.snapshots:
                    for change in group:
                        ui.diff(unified(change["path"], change["before"], change["after"]))
            else:
                ui.notice(
                    "Неизвестная команда",
                    "Введите / для выбора команды или /help для справки.",
                    "warning",
                )
        except (ApiError, ValueError, OSError) as exc:
            if isinstance(exc, ApiError) and text.startswith("/") and command != "/compact":
                session.setdefault("diagnostics", []).append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "kind": command,
                        "status": "error",
                        "request_id": exc.request_id or client.request_id,
                        "usage": None,
                    }
                )
                store.save(session)
            ui.notice("Ошибка", str(exc), "error")
        except EOFError:
            break
        except KeyboardInterrupt:
            ui.notice("Операция прервана")


def show_sessions(store: Store, ui: UI) -> None:
    ui.table(
        [(row[0], f"{row[1] or 'без модели'} · {row[2]} · {row[3]} UTC") for row in store.list()],
        "Сессии",
    )


async def run(args: argparse.Namespace) -> None:
    settings = Settings.load(args.base_url, args.model)
    ui, credentials, store = UI(settings.color), Credentials(settings.base_url), Store()
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
