"""Pydantic models for SpiritLM API payloads."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
