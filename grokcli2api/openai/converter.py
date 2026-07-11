"""Format translation between the OpenAI surface and the upstream Grok client.

The Grok CLI's chat-completion format is *almost* OpenAI-compatible (the
binary ships a struct named exactly ``ChatCompletionRequest``). The only
differences we expect to smooth out are:

* Grok expects a ``model`` field -- we forward it verbatim.
* Grok allows per-request client hints in extension fields
  (``reasoning_effort``, ``search``, etc.). We forward them too.
* Streaming chunks already look like OpenAI chunks.

This module is mostly a passthrough with explicit coercion of
``tool_call_id`` aliases and a sensible default for ``model`` if the upstream
doesn't echo one back.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from grokcli2api.openai.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from grokcli2api.utils.logger import get_logger

log = get_logger(__name__)

# Defaults used to fill the gaps when the upstream omits a field.
_DEFAULT_STREAM_MODEL = "grok-build"
_DEFAULT_CREATE_TS = 0

# Conversions from friendly OpenAI model IDs to the per-session upstream ids.
# The HAR shows the official CLI keeps using "grok-build" as the model id
# for Grok 4.5 routing; we expose a few more aliases for OpenAI callers.
_MODEL_ALIASES = {
    "grok-4": "grok-build",
    "grok-4.5": "grok-build",
    "grok-auto": "grok-build",
    "grok-build": "grok-build",
}


def _upstream_model(model: str | None) -> str:
    return _MODEL_ALIASES.get(model or "", model or _DEFAULT_STREAM_MODEL)


class GrokConverter:
    """Stateless transformer between OpenAI dicts and Grok wire dicts."""

    # ---- request side ---------------------------------------------------

    def prepare_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert an incoming OpenAI request into the dict we forward.

        The main work is hoisting everything into ``messages`` properly
        (Grok expects the OpenAI shape here, including ``name`` and
        ``tool_call_id`` on tool messages) and stripping None values that
        could confuse the proxy.
        """

        body: dict[str, Any] = {
            "model": request.model or _DEFAULT_STREAM_MODEL,
            "messages": [self._dump_message(m) for m in request.messages],
            "stream": bool(request.stream),
        }
        for source, key in (
            (request.temperature, "temperature"),
            (request.top_p, "top_p"),
            (request.presence_penalty, "presence_penalty"),
            (request.frequency_penalty, "frequency_penalty"),
            (request.n, "n"),
            (request.stop, "stop"),
            (request.user, "user"),
            (request.seed, "seed"),
        ):
            if source is not None:
                body[key] = source

        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            body["response_format"] = request.response_format

        # Passthrough any operator-defined extensions (``reasoning_effort``,
        # ``search``, ``x_grok_*`` mirrors, etc.).
        for key, value in request.model_extra.items():
            body.setdefault(key, value)

        return body

    @staticmethod
    def _dump_message(message: ChatMessage) -> dict[str, Any]:
        """Coerce a ChatMessage into the dict shape the proxy expects."""

        payload: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            payload["content"] = message.content
        if message.name:
            payload["name"] = message.name
        if message.tool_calls:
            payload["tool_calls"] = message.tool_calls
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id

        # Carry through any extras the OpenAI client might have attached
        # (e.g. ``reasoning_content`` for Kimi-style logs).
        for key, value in (message.model_extra or {}).items():
            payload.setdefault(key, value)

        return payload

    # ---- response side --------------------------------------------------

    def parse_non_stream(
        self,
        raw: Mapping[str, Any],
        *,
        fallback_model: str,
    ) -> ChatCompletionResponse:
        """Wrap a non-streaming upstream JSON dict into our schema."""

        return ChatCompletionResponse.model_validate(
            self._normalize_response(raw, fallback_model=fallback_model)
        )

    def normalize_stream_chunk(
        self,
        chunk: Mapping[str, Any],
        *,
        fallback_model: str,
    ) -> dict[str, Any]:
        """Return the JSON-shaped dict we should push to the SSE client."""

        return self._normalize_response(chunk, fallback_model=fallback_model, is_stream=True)

    # ---- private helpers ------------------------------------------------

    @staticmethod
    def _normalize_response(
        raw: Mapping[str, Any],
        *,
        fallback_model: str,
        is_stream: bool = False,
    ) -> dict[str, Any]:
        """Force each upstream object to have the OpenAI-required keys present."""

        normalized = dict(raw)
        normalized.setdefault("id", f"chatcmpl-{raw.get('id', '')}" or None)
        if not normalized.get("id"):
            # Synthesize an id when upstream omits one (it never actually does,
            # but defence-in-depth is cheap).
            import uuid
            normalized["id"] = f"chatcmpl-{uuid.uuid4().hex}"

        normalized.setdefault("object", "chat.completion.chunk" if is_stream else "chat.completion")
        normalized.setdefault("created", _DEFAULT_CREATE_TS)
        normalized.setdefault("model", raw.get("model") or fallback_model)
        normalized.setdefault("choices", [])

        # Coerce choices[i].{message|delta} dicts into proper shape.
        coerced_choices: list[dict[str, Any]] = []
        for choice in normalized["choices"]:
            coerced = dict(choice)
            coerced.setdefault("index", 0)
            if is_stream and "delta" in choice:
                delta = dict(choice["delta"])
                coerced["delta"] = delta
            elif not is_stream and "message" in choice:
                msg = dict(choice["message"])
                coerced["message"] = msg
            coerced_choices.append(coerced)
        normalized["choices"] = coerced_choices

        if "usage" in raw and raw["usage"] is not None:
            try:
                normalized["usage"] = Usage.model_validate(raw["usage"]).model_dump()
            except Exception:
                log.debug("usage payload was malformed, dropping", exc_info=True)
                normalized.pop("usage", None)

        return normalized

    # ---- batch utilities (used by tests) --------------------------------

    def iter_request(self, requests: Iterable[ChatCompletionRequest]):
        for r in requests:
            yield self.prepare_request(r)

    # ---- Responses-API target --------------------------------------------

    def prepare_responses_body(
        self,
        request: ChatCompletionRequest,
    ) -> dict[str, Any]:
        """Translate OpenAI-style request into the upstream Responses API body.

        The /v1/responses endpoint expects::

            {
              "input": [{"type": "message", "role": ..., "content": ...}, ...],
              "model": "grok-build",
              "max_output_tokens": int,
              "stream": bool,
              "temperature": float,
              "tools": [...],
              "tool_choice": ...,
              "store": false,
              "include": [...],
              "reasoning": {"summary": "concise"},
              ...
            }

        Notes:
        * ``include`` defaults to ``["reasoning.encrypted_content"]`` -- that's
          what the official CLI sends when reasoning items are involved.
        * Tools / tool_choice are passed through unchanged so callers can
          request OpenAI-compatible tool behaviour.
        """

        body: dict[str, Any] = {
            "input": [self._dump_message(m) for m in request.messages],
            "model": _upstream_model(request.model),
            "max_output_tokens": 8000,
            "stream": bool(request.stream),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"summary": "concise"},
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            body["response_format"] = request.response_format
        return body
