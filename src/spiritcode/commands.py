"""The command palette and help share one source of descriptions and arguments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    arguments: str = ""


COMMANDS = (
    Command("/help", "Все команды и сочетания клавиш"),
    Command("/model", "Поиск и смена модели", "[id]"),
    Command("/context", "Контекст, токены и диагностика сессии"),
    Command("/profile", "Профиль SpiritLM"),
    Command("/balance", "Баланс аккаунта"),
    Command("/subscription", "Подписка и доступные кредиты"),
    Command("/usage", "Статистика аккаунта", "[2d|7d|30d|all]"),
    Command("/new", "Начать новую сессию"),
    Command("/sessions", "Сохранённые сессии"),
    Command("/resume", "Продолжить сохранённую сессию", "<id>"),
    Command("/compact", "Сжать контекст отдельным оплачиваемым запросом"),
    Command("/diff", "Посмотреть правки сессии"),
    Command("/undo", "Отменить последнюю группу правок"),
    Command("/permission", "Выбрать режим разрешений"),
    Command("/permissions", "Выбрать режим разрешений (алиас)"),
    Command("/exit", "Завершить SpiritCode"),
)


def help_text() -> str:
    commands = "\n".join(
        f"{command.name}{' ' + command.arguments if command.arguments else ''} — "
        f"{command.description}"
        for command in COMMANDS
    )
    return commands + (
        '\n\n@путь или @"путь с пробелами" — прикрепить файл'
        "\nEnter — отправить; Shift+Enter — новая строка; стрелки — выбор / история"
        "\nTab / Enter в меню — принять подсказку; Esc — закрыть меню"
        "\nCtrl+C — прервать операцию; дважды в пустом вводе — выйти"
    )
