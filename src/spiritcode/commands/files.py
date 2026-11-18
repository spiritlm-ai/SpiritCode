"""Workspace edit commands."""

from spiritcode.commands.chat import Chat
from spiritcode.tools.files import unified


async def undo(chat: Chat) -> None:
    result = await chat.tools.undo()
    chat.session["messages"].append({"role": "user", "content": "Результат /undo: " + result})
    chat.store.save(chat.session)
    chat.ui.notice("Отмена правок", result)


def show_diff(chat: Chat) -> None:
    if not chat.tools.snapshots:
        chat.ui.muted("В этой сессии пока нет правок.")
    for group in chat.tools.snapshots:
        for change in group:
            chat.ui.diff(unified(change["path"], change["before"], change["after"]))
