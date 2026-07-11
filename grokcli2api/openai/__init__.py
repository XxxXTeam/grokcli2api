"""OpenAI-compatible schemas and format-translation layer.

We mirror the public OpenAI REST shape exactly. Anything outside the spec
(e.g. Grok's own ``reasoning_effort`` enum or web-search toggle) is carried
through as an extension field so existing OpenAI clients keep working but
operators can opt in to Grok-only behaviour.
"""

from grokcli2api.openai.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageDelta,
    Model,
    ModelList,
    ErrorPayload,
    ErrorResponse,
    Usage,
)
from grokcli2api.openai.converter import GrokConverter

__all__ = [
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatCompletionChoice",
    "ChatMessage",
    "ChatMessageDelta",
    "Model",
    "ModelList",
    "ErrorPayload",
    "ErrorResponse",
    "Usage",
    "GrokConverter",
]
