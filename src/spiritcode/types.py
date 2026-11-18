"""Cross-module type definitions."""

from typing import Literal

NoticeKind = Literal["info", "warning", "error", "success"]

PermissionMode = Literal["standard", "unrestricted", "full"]
PERMISSION_MODES: tuple[PermissionMode, ...] = ("full", "standard", "unrestricted")
