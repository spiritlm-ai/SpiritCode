"""Model catalog, selection, and switching."""

from decimal import Decimal

from prompt_toolkit.completion import FuzzyWordCompleter

from spiritcode.commands.chat import Chat
from spiritcode.core.agent import Agent
from spiritcode.core.api import ApiError, Client
from spiritcode.core.config import Settings
from spiritcode.core.models import Model
from spiritcode.sanitize import clean
from spiritcode.ui.interface import UI


def model_rows(models: list[Model]) -> list[tuple[str, str]]:
    rows = []
    for model in models:
        if model.price_per_million_tokens is None:
            price = "цена недоступна"
        else:
            price = f"{Decimal(model.price_per_million_tokens):.2f} ₽ / 1 млн токенов"
            if model.sample_price:
                price += " · ориентир"
        rows.append((model.id, price))
    return rows


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
    elif selected.tool_calling is False:
        ui.muted("Режим чата · модель не поддерживает инструменты. Выберите другую через /model.")
    set_model_label(ui, selected)
    return selected


async def choose_model(
    client: Client, settings: Settings, ui: UI, requested: str | None = None
) -> Model | None:
    ui.completer.models = []
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


async def apply_model(chat: Chat, model: Model) -> None:
    chat.session["model"] = model.id

    if chat.agent:
        chat.agent.model = model
        chat.agent.save()
    else:
        chat.agent = Agent(chat.client, model, chat.session, chat.store, chat.tools, chat.ui)

    chat.model = model
    chat.store.save(chat.session)
    chat.ui.reset(model.id, chat.root)

    if model.tool_calling is False:
        chat.ui.notice(
            "Режим чата",
            "Модель не поддерживает инструменты. Выберите другую через /model.",
            "warning",
        )
