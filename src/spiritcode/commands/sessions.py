"""Session switching and listing."""

import asyncio
import os
from pathlib import Path

from spiritcode.commands.chat import Chat
from spiritcode.commands.model import resolve_model
from spiritcode.core.agent import Agent
from spiritcode.core.store import Store, new_session
from spiritcode.tools.executor import Tools
from spiritcode.ui.interface import UI


async def replace_session(chat: Chat, command: str, argument: str) -> None:
    replacement = (
        chat.store.load(argument)
        if command == "/resume"
        else new_session(chat.root, chat.model.id if chat.model else "")
    )
    replacement_root = await asyncio.to_thread(Path(replacement["root"]).resolve)
    if not replacement_root.is_dir():
        raise ValueError("Каталог сессии не существует.")

    if command == "/resume":
        chat.model = await resolve_model(chat.client, chat.ui, replacement["model"])

    chat.root, chat.session = replacement_root, replacement
    chat.tools = Tools(chat.root, chat.ui, chat.session["snapshots"], chat.settings)
    chat.agent = (
        Agent(chat.client, chat.model, chat.session, chat.store, chat.tools, chat.ui)
        if chat.model
        else None
    )
    chat.store.save(chat.session)
    os.chdir(chat.root)
    chat.ui.reset(chat.model.id if chat.model else None, chat.root)


def show_sessions(store: Store, ui: UI) -> None:
    ui.table(
        [(row[0], f"{row[1] or 'без модели'} · {row[2]} · {row[3]} UTC") for row in store.list()],
        "Сессии",
    )
