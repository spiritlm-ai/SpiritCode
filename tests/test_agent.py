import json

import httpx
import pytest

from spiritcode.agent import Agent
from spiritcode.api import ApiError, Client, Model
from spiritcode.config import Settings
from spiritcode.store import Store, new_session
from spiritcode.tools import Tools
from tests.test_tools import FakeUI


class AgentUI(FakeUI):
    def begin_response(self):
        pass

    def delta(self, text):
        self.output.append(text)

    def end_response(self):
        pass


def event(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def sse(events):
    return "".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events) + (
        "data: [DONE]\r\n\r\n"
    )


def call_events(name, args, number):
    arguments = json.dumps(args)
    return [
        event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{number}",
                        "function": {
                            "name": name[:3],
                            "arguments": arguments[:4],
                        },
                    }
                ]
            }
        ),
        event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {
                            "name": name[3:],
                            "arguments": arguments[4:],
                        },
                    }
                ]
            },
            "tool_calls",
        ),
    ]


async def test_agent_finds_reads_edits_runs_and_finishes_without_at(tmp_path):
    (tmp_path / "app.py").write_text("answer = 1\n")
    turns = [
        call_events("list_files", {}, 0),
        call_events("search", {"query": "answer"}, 1),
        call_events("read_file", {"path": "app.py"}, 2),
        call_events(
            "edit_file", {"path": "app.py", "old_text": "answer = 1", "new_text": "answer = 2"}, 3
        ),
        call_events("run_command", {"command": "echo checked", "cwd": "."}, 4),
        [event({"content": "Готово"}, "stop")],
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse(turns[len(requests) - 1]))

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    tools = Tools(tmp_path, ui, session["snapshots"], Settings(permission_mode="full"))
    agent = Agent(client, Model(id="test"), session, store, tools, ui)
    try:
        await agent.turn("Исправь answer на 2 и проверь")
        assert (tmp_path / "app.py").read_text() == "answer = 2\n"
        assert len(ui.prompts) == 1
        assert len(requests) == 6
        assert session["messages"][-1]["content"] == "Готово"
        assert "checked" in session["messages"][-2]["content"]
        restored = store.load(session["id"])
        assert restored["snapshots"][0][0]["before"] == "answer = 1\n"
    finally:
        await client.close()
        store.close()


async def test_chat_only_model_never_gets_tools(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([event({"content": "Чат"}, "stop")]))

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    agent = Agent(
        client,
        Model(id="test", tool_calling=False),
        session,
        store,
        Tools(tmp_path, ui, session["snapshots"]),
        ui,
    )
    await agent.turn("Привет")
    assert "tools" not in requests[0]
    await client.close()
    store.close()


async def test_incomplete_tool_call_never_mutates_files(tmp_path):
    events = call_events("create_file", {"path": "bad", "content": "x"}, 0)
    events[-1]["choices"][0]["finish_reason"] = "length"
    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(200, content=sse(events)),
        ),
    )
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    agent = Agent(
        client,
        Model(id="test", tool_calling=True),
        session,
        store,
        Tools(tmp_path, ui, session["snapshots"]),
        ui,
    )
    with pytest.raises(ApiError):
        await agent.turn("Создай файл")
    assert not (tmp_path / "bad").exists()
    await client.close()
    store.close()


async def test_recovery_closes_pending_tools_without_execution(tmp_path):
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    session["messages"] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "pending",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": '{"command":"echo REPLAY","cwd":"."}',
                    },
                }
            ],
        }
    ]
    client = Client(Settings(), "test", httpx.MockTransport(lambda _: pytest.fail("Network")))
    Agent(client, Model(id="test"), session, store, Tools(tmp_path, ui, session["snapshots"]), ui)
    assert session["messages"][-1]["role"] == "tool"
    assert "Не повторялся" in session["messages"][-1]["content"]
    assert not ui.prompts
    await client.close()
    store.close()


async def test_compaction_archives_original_and_preserves_goal(tmp_path):
    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=sse([event({"content": "Цель: исправить вход"}, "stop")])
            ),
        ),
    )
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    session["messages"] = [
        {"role": "user", "content": "Исправь вход"},
        {"role": "assistant", "content": "Ищу"},
    ]
    agent = Agent(
        client, Model(id="test"), session, store, Tools(tmp_path, ui, session["snapshots"]), ui
    )
    assert await agent.compact()
    assert "Цель: исправить вход" in session["messages"][0]["content"]
    assert len(store.list()) == 2
    await client.close()
    store.close()


async def test_automatic_compaction_rebuilds_instructions_and_preserves_archive(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Keep this scoped rule")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        text = "Цель: исправить вход; осталось проверить" if len(requests) == 1 else "Проверено"
        return httpx.Response(200, content=sse([event({"content": text}, "stop")]))

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    session["messages"] = [{"role": "user", "content": "цель: исправить вход " * 1500}]
    agent = Agent(
        client, Model(id="test"), session, store, Tools(tmp_path, ui, session["snapshots"]), ui
    )
    await agent.turn("продолжай")
    assert len(requests) == 2
    assert "Keep this scoped rule" in requests[1]["messages"][0]["content"]
    assert "осталось проверить" in requests[1]["messages"][1]["content"]
    assert len(store.list()) == 2
    await client.close()
    store.close()


async def test_failed_compaction_keeps_history(tmp_path):
    client = Client(Settings(), "test", httpx.MockTransport(lambda _: httpx.Response(429)))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    original = [
        {"role": "user", "content": "Исправь вход"},
        {"role": "assistant", "content": "Ищу"},
    ]
    session["messages"] = list(original)
    agent = Agent(
        client, Model(id="test"), session, store, Tools(tmp_path, ui, session["snapshots"]), ui
    )
    assert not await agent.compact()
    assert session["messages"] == original
    await client.close()
    store.close()


async def test_explicit_attachment_and_model_switch_keep_conversation(tmp_path):
    (tmp_path / "context.txt").write_text("данные файла")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([event({"content": "Ответ"}, "stop")]))

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "first")
    agent = Agent(
        client,
        Model(id="first", tool_calling=False),
        session,
        store,
        Tools(tmp_path, ui, session["snapshots"]),
        ui,
    )
    await agent.turn("Проверь @context.txt")
    assert "данные файла" in requests[0]["messages"][-1]["content"]
    assert "tools" not in requests[0]
    agent.model = Model(id="second", tool_calling=True)
    await agent.turn("Продолжай")
    assert requests[1]["model"] == "second"
    assert "tools" in requests[1]
    assert "данные файла" in requests[1]["messages"][1]["content"]
    await client.close()
    store.close()


@pytest.mark.parametrize("usage", [None, {"total_tokens": 0}, {"total_tokens": 21}])
async def test_usage_persisted_separately_without_automatic_output(tmp_path, usage):
    events = [event({"content": "Ответ"}, "stop")]
    if usage is not None:
        events.append({"usage": usage})
    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"x-request-id": "diagnostic-request"},
                content=sse(events),
            )
        ),
    )
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    agent = Agent(client, Model(id="test"), session, store, Tools(tmp_path, ui, []), ui)
    try:
        await agent.turn("Привет")
        diagnostic = store.load(session["id"])["diagnostics"][-1]
        assert diagnostic["usage"] == usage
        assert diagnostic["request_id"] == "diagnostic-request"
        assert diagnostic["status"] == "completed"
        assert "diagnostic-request" not in str(ui.output)
        assert "Токены:" not in str(ui.output)
        assert "diagnostic-request" not in str(session["messages"])
    finally:
        await client.close()
        store.close()


async def test_failed_and_interrupted_requests_keep_diagnostics(tmp_path):
    import asyncio

    client = Client(
        Settings(),
        "test",
        httpx.MockTransport(
            lambda _: httpx.Response(
                429,
                headers={"x-request-id": "failed-request"},
            )
        ),
    )
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    agent = Agent(client, Model(id="test"), session, store, Tools(tmp_path, ui, []), ui)
    try:
        with pytest.raises(ApiError):
            await agent.response([])
        diagnostic = store.load(session["id"])["diagnostics"][-1]
        assert diagnostic["status"] == "error"
        assert diagnostic["usage"] is None
        assert diagnostic["request_id"] == "failed-request"

        async def interrupted_stream(payload):
            client.request_id = "interrupted-request"
            yield {"usage": {"total_tokens": 3}}
            raise asyncio.CancelledError

        client.stream = interrupted_stream
        with pytest.raises(asyncio.CancelledError):
            await agent.response([])
        diagnostic = store.load(session["id"])["diagnostics"][-1]
        assert diagnostic["status"] == "cancelled"
        assert diagnostic["usage"]["total_tokens"] == 3
        assert diagnostic["request_id"] == "interrupted-request"
    finally:
        await client.close()
        store.close()


async def test_tool_rejection_explained_without_retry(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "private upstream response"})

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store, ui = Store(tmp_path / "sessions.db"), AgentUI()
    session = new_session(tmp_path, "test")
    agent = Agent(client, Model(id="test"), session, store, Tools(tmp_path, ui, []), ui)
    try:
        with pytest.raises(ApiError, match="/model") as error:
            await agent.turn("Проверь проект")
        assert "private" not in str(error.value)
        assert len(requests) == 1
        assert "tools" in requests[0]
        assert session["diagnostics"][-1]["status"] == "error"
    finally:
        await client.close()
        store.close()
