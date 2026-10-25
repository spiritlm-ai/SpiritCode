import io
import sqlite3
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from rich.console import Console

from spiritcode.agent import Agent
from spiritcode.api import ApiError, Client, Model
from spiritcode.cli import account, interactive, load_account_label, model_rows, show_context
from spiritcode.config import Settings
from spiritcode.store import Store
from spiritcode.tools import Tools
from spiritcode.ui import UI
from tests.test_agent import event, sse


def captured_ui():
    output = io.StringIO()
    ui = UI(color=False)
    ui.console = Console(file=output, width=120, no_color=True)
    return ui, output


async def test_account_rounds_nested_amounts_without_changing_data_or_printing_links():
    payload = {
        "balance": "23609.00000000",
        "subscription_credits_used": "0.09271650",
        "username": "123.456789",
        "current": {
            "credits": "500.00000000",
            "remainingCredits": "12.34567890",
            "startsAt": "2026-10-24T00:00:00.000Z",
        },
        "upcoming": [{"credits": "999999999999999.99999999"}],
        "total_tokens": None,
        "requests": 7,
    }
    client = Client(Settings(), "test")
    client.get = AsyncMock(return_value=payload)
    ui, output = captured_ui()
    try:
        await account(client, ui)
        text = output.getvalue()
        for expected in ("23609.00", "0.09", "500.00", "12.35", "1000000000000000.00"):
            assert expected in text
        assert "24.10.2026 00:00 UTC" in text
        assert "123.456789" in text
        assert "нет данных" in text
        assert "dashboard" not in text
        assert payload["balance"] == "23609.00000000"
        assert payload["requests"] == 7
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("command", "payload", "expected"),
    [
        (
            "profile",
            {
                "name": "Анна",
                "created_at": "2026-10-20T19:32:50.337Z",
                "key": {
                    "name": "spiritcode",
                    "masked": "[ключ скрыт]…",
                    "use_subscription": True,
                    "allow_balance_fallback": False,
                },
            },
            [
                "20.10.2026 19:32 UTC",
                "Название",
                "spiritcode",
                "Использовать подписку",
                "да",
                "Резервный баланс",
                "нет",
                "[ключ скрыт]…",
            ],
        ),
        (
            "subscription",
            {
                "current": {
                    "planName": "Nova",
                    "mode": "live",
                    "credits": "645.00",
                    "remainingCredits": "644.91",
                    "reservedCredits": "0.00",
                    "startsAt": "2026-10-23T14:06:08.812Z",
                    "endsAt": "2026-11-22T14:06:08.812Z",
                    "requestsPerMinute": 10,
                    "concurrentRequests": 1,
                },
                "upcoming": [{"planName": "Vega", "credits": "100.00"}],
                "running": 0,
                "recent": 0,
            },
            [
                "Тариф",
                "Nova",
                "Основной",
                "Кредиты",
                "645.00",
                "Осталось кредитов",
                "644.91",
                "В резерве",
                "0.00",
                "23.10.2026 14:06 UTC",
                "22.11.2026 14:06 UTC",
                "Запросов в минуту",
                "10",
                "Одновременных запросов",
                "Следующие периоды",
                "Vega",
                "100.00",
            ],
        ),
        (
            "subscription",
            {"current": None, "upcoming": [], "running": 0, "recent": 0},
            ["нет активной подписки", "нет запланированных периодов", "0"],
        ),
    ],
)
async def test_account_displays_readable_fields_instead_of_json(command, payload, expected):
    client = Client(Settings(), "test")
    client.get = AsyncMock(return_value=payload)
    ui, output = captured_ui()
    try:
        await account(client, ui, command)
        text = output.getvalue()
        for value in expected:
            assert value in text
        for raw in ("{", "}", "[\n", '"planName"', '"use_subscription"', "true", "false"):
            assert raw not in text
        client.get.assert_awaited_once_with(command)
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("profile", "subscription", "expected"),
    [
        ({"username": "anna", "name": "Анна"}, {"current": None}, "@anna | Free"),
        ({"username": "anna"}, {"current": {"planName": "Vega"}}, "@anna | Vega"),
        ({"name": "Анна"}, {"current": None}, "Анна | Free"),
        ({"username": "anna"}, ApiError("Нет связи"), "@anna | тариф недоступен"),
        (ApiError("Нет связи"), {"current": None}, "Пользователь | Free"),
    ],
)
async def test_account_header_uses_server_identity_and_current_plan(
    profile, subscription, expected
):
    client = Client(Settings(), "test")
    client.get = AsyncMock(side_effect=[profile, subscription])
    ui, _ = captured_ui()
    try:
        await load_account_label(client, ui)
        assert ui.account_label == expected
    finally:
        await client.close()


def test_catalog_formats_prices_and_keeps_unknown_separate_from_zero():
    models = [
        Model(id="rounded", price_per_million_tokens="11.80900000"),
        Model(id="zero", price_per_million_tokens="0.00000000"),
        Model(id="unknown"),
    ]
    assert model_rows(models) == [
        ("rounded", "11.81 RUB / 1 MTok"),
        ("zero", "0.00 RUB / 1 MTok"),
        ("unknown", "неизвестно RUB / 1 MTok"),
    ]


async def test_model_and_new_chat_reset_screen_without_losing_selected_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("spiritcode.config.config_path", lambda: tmp_path / "config.toml")
    client = Client(Settings(), "test")
    client.models = AsyncMock(return_value=[Model(id="test")])
    store = Store(tmp_path / "sessions.db")
    ui, _ = captured_ui()
    ui.reset = Mock(wraps=ui.reset)
    ui.ask = AsyncMock(side_effect=["/model test", "/new", "/exit"])
    try:
        await interactive(client, client.settings, ui, store)
        assert ui.reset.call_count == 2
        ui.reset.assert_called_with("test", tmp_path)
        sessions = [store.load(row[0]) for row in store.list()]
        assert len(sessions) == 2
        assert all(session["model"] == "test" for session in sessions)
        assert all(session["messages"] == [] for session in sessions)
    finally:
        await client.close()
        store.close()


async def test_chat_opens_without_catalog_and_requires_explicit_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = Client(Settings(), "test", httpx.MockTransport(lambda _: pytest.fail("Network")))
    store = Store(tmp_path / "sessions.db")
    ui, output = captured_ui()
    ui.ask = AsyncMock(side_effect=["Привет", "/help", "/exit"])
    try:
        await interactive(client, Settings(), ui, store)
        text = output.getvalue()
        assert "SpiritCode" in text
        assert "SpiritCode CLI" not in text
        assert "/ — команды" not in text
        assert "Выберите /model" in text
        assert "Контекст, токены и диагностика" in text
        session = store.load(store.list()[0][0])
        assert session["model"] == ""
        assert session["messages"] == []
        assert session["id"] not in text
        assert "Request ID" not in text
    finally:
        await client.close()
        store.close()


async def test_model_selection_chat_and_context_are_separate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("spiritcode.config.config_path", lambda: tmp_path / "config.toml")
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test"}]})
        if request.url.path == "/v1/spiritcode/models":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"x-request-id": "request-visible-only-in-context"},
            content=sse(
                [
                    event({"content": "Готово"}, "stop"),
                    {"usage": {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}},
                ]
            ),
        )

    client = Client(Settings(), "test", httpx.MockTransport(handler))
    store = Store(tmp_path / "sessions.db")
    ui, output = captured_ui()
    ui.ask = AsyncMock(side_effect=["/model test", "Привет", "/exit"])
    try:
        await interactive(client, client.settings, ui, store)
        session = store.load(store.list()[0][0])
        assert requests == ["/v1/models", "/v1/spiritcode/models", "/v1/chat/completions"]
        assert session["messages"][-1]["content"] == "Готово"
        assert "request-visible-only-in-context" not in output.getvalue()
        assert session["id"] not in output.getvalue()
        assert "Токены:" not in output.getvalue()
        agent = Agent(client, Model(id="test"), session, store, Tools(tmp_path, ui, []), ui)
        output.seek(0)
        output.truncate()
        await show_context(session, agent, ui)
        text = output.getvalue()
        assert "request-visible-only-in-context" in text
        assert session["id"] in text
        assert "Выходные токены (сервер)" in text
        assert session["diagnostics"][-1]["usage"]["completion_tokens"] == 0
        assert (tmp_path / "config.toml").is_file()
    finally:
        await client.close()
        store.close()


@pytest.mark.parametrize("available", [True, False])
async def test_configured_model_never_prompts_on_startup(tmp_path, monkeypatch, available):
    monkeypatch.chdir(tmp_path)
    settings = Settings(model="test")
    client = Client(settings, "test")
    client.models = AsyncMock(return_value=[Model(id="test")] if available else [])
    store = Store(tmp_path / "sessions.db")
    ui, output = captured_ui()
    ui.ask = AsyncMock(side_effect=["/exit"])
    try:
        await interactive(client, settings, ui, store)
        ui.ask.assert_awaited_once_with(chat=True)
        assert "Модели SpiritLM" not in output.getvalue()
        assert ("Модель недоступна" in output.getvalue()) != available
    finally:
        await client.close()
        store.close()


async def test_cancelled_model_picker_keeps_chat_without_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = Client(Settings(), "test")
    client.models = AsyncMock(return_value=[Model(id="test")])
    store = Store(tmp_path / "sessions.db")
    ui, output = captured_ui()
    ui.ask = AsyncMock(side_effect=["/model", "", "/exit"])
    try:
        await interactive(client, Settings(), ui, store)
        assert store.load(store.list()[0][0])["model"] == ""
        assert "Модель выбрана" not in output.getvalue()
    finally:
        await client.close()
        store.close()


async def test_failed_catalog_request_id_only_shown_in_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = Client(Settings(), "test")
    client.models = AsyncMock(side_effect=ApiError("Нет связи", request_id="catalog-secret-id"))
    store = Store(tmp_path / "sessions.db")
    ui, output = captured_ui()
    ui.ask = AsyncMock(side_effect=["/model", "/exit"])
    try:
        await interactive(client, Settings(), ui, store)
        assert "catalog-secret-id" not in output.getvalue()
        session = store.load(store.list()[0][0])
        await show_context(session, None, ui)
        assert "catalog-secret-id" in output.getvalue()
    finally:
        await client.close()
        store.close()


async def test_context_handles_old_session_without_diagnostics(tmp_path):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, root TEXT, model TEXT, messages TEXT, snapshots TEXT, updated TEXT
        )""")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            (
                "old-session",
                str(tmp_path),
                "test",
                "[]",
                "[]",
                "2026-01-01",
            ),
        )
    store = Store(database)
    ui, output = captured_ui()
    try:
        session = store.load("old-session")
        assert session["diagnostics"] == []
        await show_context(session, None, ui)
        assert "нет данных" in output.getvalue()
        assert "old-session" in output.getvalue()
    finally:
        store.close()


async def test_header_precedes_first_login_prompt(tmp_path, monkeypatch):
    from argparse import Namespace

    from spiritcode.cli import run

    monkeypatch.chdir(tmp_path)
    ui, output = captured_ui()

    async def ask(*args, **kwargs):
        assert "SpiritCode" in output.getvalue()
        raise EOFError

    ui.ask = ask
    store = Store(tmp_path / "sessions.db")
    with (
        patch("spiritcode.cli.UI", return_value=ui),
        patch("spiritcode.cli.Store", return_value=store),
        patch("spiritcode.cli.Settings.load", return_value=Settings()),
        patch("spiritcode.cli.Credentials") as credentials,
    ):
        credentials.return_value.get.return_value = None
        with pytest.raises(EOFError):
            await run(Namespace(base_url=None, model=None, command=None))


@pytest.mark.parametrize("command", ["/permission", "/permissions"])
async def test_permission_command_preserved_across_model_and_sessions(
    tmp_path, monkeypatch, command
):
    from spiritcode.store import new_session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("spiritcode.config.config_path", lambda: tmp_path / "config.toml")
    settings = Settings()
    client = Client(settings, "test")
    client.models = AsyncMock(return_value=[Model(id="test")])
    store = Store(tmp_path / "sessions.db")
    previous = new_session(tmp_path, "test")
    store.save(previous)
    ui, _ = captured_ui()
    ui.ask = AsyncMock(
        side_effect=[command, "/model test", "/new", "/resume " + previous["id"], "/exit"]
    )
    ui.choose_permissions = AsyncMock(return_value=("unrestricted", False))
    from spiritcode.cli import Tools as RealTools

    instances = []

    def make_tools(*args):
        tool = RealTools(*args)
        instances.append(tool)
        return tool

    try:
        with patch("spiritcode.cli.Tools", side_effect=make_tools):
            await interactive(client, settings, ui, store)
        assert len(instances) == 3
        assert all(tool.settings is settings for tool in instances)
        assert settings.permission_mode == "unrestricted"
        assert not settings.confirm_deletion
        assert Settings.load().permission_mode == "unrestricted"
        assert not Settings.load().confirm_deletion
    finally:
        await client.close()
        store.close()
