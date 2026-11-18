"""Shared text sanitization for terminal output and persisted data."""

import re

ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")

CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

KEY = re.compile(r"sk-spiritlm-[A-Za-z0-9_-]+")


def clean(value: str) -> str:
    return CONTROLS.sub("", ANSI.sub("", KEY.sub("[ключ скрыт]", value)))
