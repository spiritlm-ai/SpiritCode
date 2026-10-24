"""Presentation-only formatting; API values and accounting keep their precision."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

DECIMAL_TEXT = re.compile(r"-?\d+\.\d+\Z")
AMOUNT_FIELDS = frozenset(
    {
        "balance",
        "cost",
        "amount",
        "credits",
        "remainingCredits",
        "reservedCredits",
        "chargedCost",
        "reservedCost",
        "pricePerMillion",
        "price_per_million_tokens",
        "subscription_credits_used",
    }
)

ACCOUNT_LABELS = {
    "id": "ID",
    "name": "Имя",
    "username": "Имя пользователя",
    "email": "Email",
    "created_at": "Регистрация",
    "key": "Текущий ключ",
    "masked": "Ключ",
    "use_subscription": "Использовать подписку",
    "allow_balance_fallback": "Резервный баланс",
    "balance": "Баланс",
    "currency": "Валюта",
    "current": "Текущая подписка",
    "upcoming": "Следующие периоды",
    "planId": "ID тарифа",
    "planName": "Тариф",
    "mode": "Режим",
    "credits": "Кредиты",
    "remainingCredits": "Осталось кредитов",
    "reservedCredits": "В резерве",
    "startsAt": "Начало периода",
    "endsAt": "Конец периода",
    "requestsPerMinute": "Запросов в минуту",
    "concurrentRequests": "Одновременных запросов",
    "running": "Активные запросы",
    "recent": "Запросы за минуту",
    "cost": "Расход RUB",
    "subscription_credits_used": "Использовано кредитов",
    "requests": "Запросы",
    "unknown_usage_count": "Запросы без данных о токенах",
    "known_tokens": "Известные токены",
    "total_tokens": "Всего токенов",
    "period": "Период",
}


def account_rows(value: Mapping[str, object]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for field, item in value.items():
        rows.extend(_account_field_rows(field, item))
    return rows


def _account_field_rows(
    field: str, value: object, indent: str = "", parent: str = ""
) -> list[tuple[str, str]]:
    label = "Название" if parent == "key" and field == "name" else ACCOUNT_LABELS.get(field, field)
    label = indent + label
    if isinstance(value, dict):
        rows = [(label, "")]
        for key, item in value.items():
            rows.extend(_account_field_rows(key, item, indent + "  ", field))
        return rows if value else [(label, "нет данных")]
    if isinstance(value, list):
        if not value:
            return [
                (label, "нет запланированных периодов" if field == "upcoming" else "нет данных")
            ]
        rows = [(label, "")]
        for index, item in enumerate(value, start=1):
            entry = f"Период {index}" if field == "upcoming" else str(index)
            rows.extend(_account_field_rows(entry, item, indent + "  ", field))
        return rows
    if value is None:
        text = "нет активной подписки" if field == "current" else "нет данных"
    elif isinstance(value, bool):
        text = "да" if value else "нет"
    elif isinstance(value, str) and field in {"created_at", "startsAt", "endsAt"}:
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            text = date.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
        except ValueError:
            text = value
    elif field == "mode" and value == "live":
        text = "Основной"
    else:
        text = str(format_account_value(value, field))
    return [(label, text)]


def format_account_value(value: object, field: str) -> object:
    if isinstance(value, dict):
        return {key: format_account_value(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [format_account_value(item, field) for item in value]
    if isinstance(value, (float, Decimal)):
        return f"{Decimal(str(value)):.2f}"
    if isinstance(value, str) and field in AMOUNT_FIELDS and DECIMAL_TEXT.fullmatch(value):
        return f"{Decimal(value):.2f}"
    return value
