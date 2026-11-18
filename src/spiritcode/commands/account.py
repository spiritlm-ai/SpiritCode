"""SpiritLM account and plan display."""

import asyncio
from typing import Any

from spiritcode.constants import ACCOUNT_TIMEOUT
from spiritcode.core.api import ApiError, Client
from spiritcode.sanitize import clean
from spiritcode.ui.display import account_rows
from spiritcode.ui.interface import UI


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
