"""Context, token budget, and request diagnostics."""

import asyncio
from typing import Any

from spiritcode.constants import DEFAULT_OUTPUT_TOKENS
from spiritcode.core.agent import Agent
from spiritcode.core.context import estimate_tokens, input_budget, system_context
from spiritcode.ui.interface import UI

DIAGNOSTIC_STATUSES = {"completed": "завершён", "error": "ошибка", "cancelled": "прерван"}

DIAGNOSTIC_KINDS = {"generation": "генерация", "compaction": "сжатие"}


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
        usage = latest.get("usage") or {}
        rows.extend(
            [
                ("Последний запрос", DIAGNOSTIC_KINDS.get(latest["kind"], latest["kind"])),
                ("Статус", DIAGNOSTIC_STATUSES.get(latest["status"], latest["status"])),
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
