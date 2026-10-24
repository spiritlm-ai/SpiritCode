import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from spiritcode.config import MAX_SSE_EVENT, REQUEST_TIMEOUT, Settings


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    tool_calling: bool | None = Field(default=None, strict=True)
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    price_per_million_tokens: str | None = None
    sample_price: bool = True


class ToolFragment(BaseModel):
    index: int = Field(strict=True, ge=0, le=127)
    id: str | None = None
    function: dict[str, str | None] = Field(default_factory=dict)


class Delta(BaseModel):
    content: str | None = None
    tool_calls: list[ToolFragment] = Field(default_factory=list)


class Choice(BaseModel):
    delta: Delta = Field(default_factory=Delta)
    finish_reason: str | None = None


class StreamEvent(BaseModel):
    choices: list[Choice] = Field(default_factory=list)
    usage: dict[str, Any] | None = None


class Client:
    def __init__(
        self, settings: Settings, key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.key = key
        self.request_id: str | None = None
        self.http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}", "Cache-Control": "no-store"},
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    def check(self, response: httpx.Response) -> None:
        self.request_id = response.headers.get("x-request-id")
        if response.is_success:
            return
        messages = {
            401: "Неверный или отозванный API-ключ SpiritLM.",
            402: "Недостаточно средств или кредитов подписки SpiritLM.",
            403: "SpiritLM запретил операцию. Проверьте доступ и настройки ключа.",
            404: "Эта функция ещё не доступна на сервере SpiritLM.",
            429: "Достигнут лимит SpiritLM. Повторите запрос позже.",
        }
        raise ApiError(
            messages.get(response.status_code, "Ошибка сервера SpiritLM."),
            response.status_code,
            self.request_id,
        )

    async def get(self, name: str, **params: str) -> Any:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self.http.get(self.settings.url(name), params=params)
                self.check(response)
                return response.json()
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise ApiError("Не удалось получить корректный ответ SpiritLM.") from exc

    async def models(self) -> list[Model]:
        payload = await self.get("models")
        models = [Model.model_validate(item) for item in payload["data"]]
        try:
            metadata = {item["id"]: item for item in (await self.get("metadata"))["data"]}
        except ApiError as exc:
            if exc.status not in {404, 501}:
                raise
            metadata = {}
        return [Model.model_validate({**m.model_dump(), **metadata.get(m.id, {})}) for m in models]

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """HTTPX incrementally decodes UTF-8 and normalizes CRLF in aiter_lines."""
        self.request_id = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self.http.stream(
                    "POST",
                    self.settings.url("chat"),
                    json={**payload, "stream": True},
                ) as response:
                    self.check(response)
                    data: list[str] = []
                    size = 0
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                            size += len(line)
                            if size > MAX_SSE_EVENT:
                                raise ApiError("Слишком большое событие SpiritLM.")
                        elif not line and data:
                            event = "\n".join(data)
                            data, size = [], 0
                            if event == "[DONE]":
                                return
                            value = json.loads(event)
                            if not isinstance(value, dict) or "error" in value:
                                raise ApiError("SpiritLM сообщил об ошибке в потоке.")
                            # Validate framing before any consumer can act on an event.
                            yield StreamEvent.model_validate(value).model_dump(exclude_none=True)
                    raise ApiError("Поток SpiritLM оборвался до завершения ответа.")
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise ApiError(
                "Соединение SpiritLM прервано или истекло время запроса.",
                request_id=self.request_id,
            ) from exc
