"""OpenAI-compatible Pydantic v2 schemas.

Limited to the surface required for ``/v1/chat/completions`` and
``/v1/models``. Field aliases match the JSON names that OpenAI uses, so
clients can POST us the exact same payload they would send to OpenAI.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class ChatMessage(_Base):
    """One message in the conversation.

    ``content`` is intentionally typed as ``Any`` because OpenAI now permits
    either a plain string or a list of typed parts (text / image_url / etc.).
    """

    role: str
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = Field(default=None, alias="tool_call_id")


class ChatCompletionRequest(_Base):
    """POST body for ``/v1/chat/completions``."""

    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = Field(default=None, alias="top_p")
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Any] = None
    presence_penalty: Optional[float] = Field(default=None, alias="presence_penalty")
    frequency_penalty: Optional[float] = Field(default=None, alias="frequency_penalty")
    user: Optional[str] = None
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Any] = Field(default=None, alias="tool_choice")
    response_format: Optional[dict[str, Any]] = Field(default=None, alias="response_format")
    seed: Optional[int] = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class Usage(_Base):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatMessageDelta(_Base):
    """Streaming delta payload -- keeps ``role`` optional because first chunks only have content."""

    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class ChatCompletionChoice(_Base):
    index: int
    message: Optional[ChatMessage] = None
    delta: Optional[ChatMessageDelta] = None
    finish_reason: Optional[str] = None


class ChatCompletionResponse(_Base):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionChoice]
    usage: Optional[Usage] = None
    system_fingerprint: Optional[str] = Field(default=None, alias="system_fingerprint")


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------


class Model(_Base):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "xai"


class ModelList(_Base):
    object: str = "list"
    data: list[Model]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorPayload(_Base):
    message: str
    type: str = "invalid_request_error"
    param: Optional[str] = None
    code: Optional[str] = None


class ErrorResponse(_Base):
    error: ErrorPayload


def make_error(message: str, *, type_: str = "invalid_request_error", code: Optional[str] = None) -> dict[str, Any]:
    return ErrorResponse(error=ErrorPayload(message=message, type=type_, code=code)).model_dump(exclude_none=True)
