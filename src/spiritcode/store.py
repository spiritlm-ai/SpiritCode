import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from spiritcode.config import data_path
from spiritcode.ui import KEY


class Store:
    def __init__(self, path: Path | None = None) -> None:
        path = path or data_path() / "sessions.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        if os.name != "nt":
            path.chmod(0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, root TEXT NOT NULL, model TEXT NOT NULL,
            messages TEXT NOT NULL, snapshots TEXT NOT NULL, updated TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS session_diagnostics (
            session_id TEXT PRIMARY KEY, requests TEXT NOT NULL
        )""")

    def save(self, session: dict[str, Any]) -> None:
        # Credentials never belong in persisted messages, tool output, or snapshots.
        self.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (
                session["id"],
                session["root"],
                session["model"],
                KEY.sub("[ключ скрыт]", json.dumps(session["messages"], ensure_ascii=False)),
                KEY.sub("[ключ скрыт]", json.dumps(session["snapshots"], ensure_ascii=False)),
            ),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO session_diagnostics VALUES (?, ?)",
            (
                session["id"],
                KEY.sub("[ключ скрыт]", json.dumps(session.get("diagnostics", []))),
            ),
        )
        self.db.commit()

    def load(self, session_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Сессия не найдена. Укажите полный ID из /sessions.")
        diagnostics = self.db.execute(
            "SELECT requests FROM session_diagnostics WHERE session_id = ?", (session_id,)
        ).fetchone()
        return {
            "id": row[0],
            "root": row[1],
            "model": row[2],
            "messages": json.loads(row[3]),
            "snapshots": json.loads(row[4]),
            "diagnostics": json.loads(diagnostics[0]) if diagnostics else [],
        }

    def list(self) -> list[tuple[str, str, str, str]]:
        return self.db.execute(
            "SELECT id, model, root, updated FROM sessions ORDER BY updated DESC LIMIT 50",
        ).fetchall()

    def close(self) -> None:
        self.db.close()


def new_session(root: Path, model: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "root": str(root),
        "model": model,
        "messages": [],
        "snapshots": [],
        "diagnostics": [],
    }
