"""State shared by the interactive chat command handlers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spiritcode.core.agent import Agent
from spiritcode.core.api import Client
from spiritcode.core.config import Settings
from spiritcode.core.models import Model
from spiritcode.core.store import Store
from spiritcode.tools.executor import Tools
from spiritcode.ui.interface import UI


@dataclass
class Chat:
    client: Client
    settings: Settings
    ui: UI
    store: Store
    root: Path
    session: dict[str, Any]
    model: Model | None
    tools: Tools
    agent: Agent | None
