import asyncio

import httpx
import pytest

from spiritcode.api import ApiError, Client
from spiritcode.config import Settings, validate_base_url


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        for byte in self.data:
            yield bytes([byte])


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/v1",
        "https://user:pass@example.com",
        "ftp://localhost",
        "https://example.com?secret=1",
    ],
)
def test_insecure_or_ambiguous_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_loopback_and_https():
    assert validate_base_url("http://[::1]:3000/") == "http://[::1]:3000"
    assert Settings().url("chat").count("/v1/") == 1


async def test_stream_fragmented_utf8_crlf_and_done():
    body = 'data: {"choices":[{"delta":{"content":"Привет"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=Chunks(body.encode()), headers={"x-request-id": "req"}
            ),
        ),
    )
    try:
        events = [event async for event in client.stream({})]
        assert events[0]["choices"][0]["delta"]["content"] == "Привет"
        assert client.request_id == "req"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body",
    ['data: {"error":{"message":"PRIVATE"}}\n\n', 'data: {"choices":[]}\n\n', "data: invalid\n\n"],
)
async def test_broken_streams_never_retry(body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as error:
            _ = [event async for event in client.stream({})]
        assert "PRIVATE" not in str(error.value)
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 302])
async def test_statuses_and_redirect_do_not_leak_or_retry(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "https://evil.example"}, text="PRIVATE sk-spiritlm-raw"
        )

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as error:
            await client.get("models")
        assert "PRIVATE" not in str(error.value)
        assert len(requests) == 1
        assert requests[0].url.host == "localhost"
    finally:
        await client.close()


async def test_old_server_models_fall_back_to_chat():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test"}]})
        return httpx.Response(404)

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    try:
        assert (await client.models())[0].tool_calling is None
    finally:
        await client.close()


async def test_cancel_closes_stream_without_retry():
    closed = []
    entered = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.append(True)

    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Waiting()),
        ),
    )

    async def consume():
        return [event async for event in client.stream({})]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    await client.close()


async def test_metadata_is_confirmed_not_inferred():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "x"}]})
        return httpx.Response(
            200, json={"data": [{"id": "x", "tool_calling": True, "context_window": 64000}]}
        )

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    assert (await client.models())[0].tool_calling is True
    await client.close()


async def test_total_stream_timeout_never_retries(monkeypatch):
    monkeypatch.setattr("spiritcode.api.REQUEST_TIMEOUT", 0.01)
    calls = []

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Waiting())

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        _ = [event async for event in client.stream({})]
    assert len(calls) == 1
    await client.close()
