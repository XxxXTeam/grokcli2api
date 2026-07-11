"""Asynchronous HTTP client that talks to ``cli-chat-proxy.grok.com``.

Why this exists:

The official Grok CLI ships with a thin client that calls
``https://cli-chat-proxy.grok.com/v1/chat/completions`` (and a few sibling
endpoints) using a bearer token + a fixed set of ``x-grok-*`` headers.

This module replicates that conversation in pure Python so we can re-use it
without booting the Rust binary. It is intentionally OpenAI-shaped on the
**outside** (``grok.ChatCompletionRequest`` matches OpenAI's schema) because
that's the format ``cli-chat-proxy.grok.com`` already speaks.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Mapping, Optional

import httpx

from grokcli2api.auth.session import SessionStore
from grokcli2api.config import Settings
from grokcli2api.grok.headers import (
    build_grok_headers,
    chat_user_agent,
    default_user_agent,
    hostname as hostname_of,
    new_agent_id,
    new_req_id,
    new_session_id_v7,
    new_traceparent,
)
from grokcli2api.utils.logger import get_logger

log = get_logger(__name__)


def _build_proxy_arg(settings: Settings):
    """Translate :class:`Settings` proxy knobs into the value ``httpx.AsyncClient`` expects.

    * No ``GROK_PROXY_URL`` set → returns ``None`` so httpx falls back to its
      built-in env-var lookup of ``HTTP_PROXY`` / ``HTTPS_PROXY`` /
      ``ALL_PROXY`` / ``NO_PROXY``.
    * ``GROK_PROXY_URL`` set → returns an :class:`httpx.Proxy` so httpx
      *always* honours it regardless of the process environment.
    * When ``GROK_NO_PROXY`` is set we *also* mirror it onto the
      ``NO_PROXY`` environment variable so httpx's URL-level bypass logic
      picks it up.
    """

    explicit = (settings.grok_proxy_url or "").strip()
    excludes = settings.no_proxy_patterns or []

    if not explicit:
        # Defer entirely to httpx's built-in env-var lookup.
        # Still propagate the no-proxy list if the operator set one, so
        # existing env-var based proxying can be restricted.
        _sync_no_proxy_env(excludes)
        return None

    _sync_no_proxy_env(excludes)
    return httpx.Proxy(url=explicit)


_NO_PROXY_ENV_KEY = "NO_PROXY"


def _sync_no_proxy_env(patterns: list[str]) -> None:
    """Mirror ``GROK_NO_PROXY`` onto ``$NO_PROXY`` (the env var httpx actually reads).

    Append to, rather than replace, any pre-existing value so users who
    configured a global bypass list aren't surprised. The patterns are added
    in the canonical comma-separated form.
    """

    if not patterns:
        return
    import os
    existing = os.environ.get(_NO_PROXY_ENV_KEY, "").strip()
    extras = [p for p in patterns if p and p not in existing.split(",")]
    if not extras:
        return
    merged = ",".join(filter(None, [existing, *extras]))
    os.environ[_NO_PROXY_ENV_KEY] = merged


class GrokAPIError(RuntimeError):
    """Raised when the upstream returns a non-2xx response.

    Carries the structured upstream error JSON when available — the
    ``cli-chat-proxy.grok.com`` endpoint returns ``{"code": "...",
    "error": "..."}`` for business-logic rejections (402 rate-limit,
    426 version gate, etc.), which the caller can surface verbatim to
    the end user so they see actionable hints (e.g. subscription URLs).
    """

    def __init__(
        self,
        *,
        status: int,
        body: str,
        request_id: Optional[str] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.request_id = request_id

        # Try to parse the upstream structured error envelope. Anything
        # we can't parse lives on as the raw `body` string.
        self.upstream_code: Optional[str] = None
        self.upstream_message: Optional[str] = None
        try:
            import json as _json
            parsed = _json.loads(body)
            if isinstance(parsed, dict):
                code = parsed.get("code")
                if isinstance(code, str):
                    self.upstream_code = code
                msg = parsed.get("error")
                if isinstance(msg, str):
                    self.upstream_message = msg
        except (ValueError, TypeError):
            pass

        super().__init__(self._format_message())

    def _format_message(self) -> str:
        parts = [f"upstream grok {self.status}"]
        if self.upstream_code:
            parts.append(f"code={self.upstream_code}")
        if self.upstream_message:
            parts.append(self.upstream_message)
        elif self.body:
            parts.append(self.body[:200])
        return " | ".join(parts)


class GrokClient:
    """Async client to the Grok CLI chat proxy.

    Usage::

        client = GrokClient.from_settings(settings, session_store)
        async with client:
            async for chunk in client.stream_chat(request_body):
                print(chunk)

    Streaming responses are surfaced as raw OpenAI-shaped chunks (the proxy
    already emits them in that shape).
    """

    def __init__(
        self,
        *,
        base_url: str,
        version: str,
        settings: Settings,
        session_store: SessionStore,
        timeout_seconds: float = 90.0,
        http2: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._version = version.strip("/")
        self._settings = settings
        self._session_store = session_store

        proxy = _build_proxy_arg(settings)
        log.debug(
            "GrokClient: proxy=%s no_proxy=%s",
            settings.grok_proxy_url or "<env-default>",
            settings.no_proxy_patterns or "[]",
        )

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            http2=http2,
            proxy=proxy,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0, read=timeout_seconds, write=30.0),
            verify=not settings.grok_tls_insecure_skip_verify,
            follow_redirects=False,
            headers={
                "accept-encoding": "gzip, br",
                # default User-Agent echoes the official compound form; some
                # paths override with the single-component chat variant.
                "user-agent": default_user_agent(
                    settings.grok_client_identifier,
                    settings.grok_client_version,
                ),
            },
        )

        # Stable ids reused across turns so the upstream sees a coherent session.
        self._agent_id = new_agent_id()
        self._session_id = new_session_id_v7()

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings, session_store: SessionStore) -> "GrokClient":
        return cls(
            base_url=settings.chat_proxy_base_url,
            version=settings.grok_chat_proxy_version,
            settings=settings,
            session_store=session_store,
        )

    @classmethod
    def from_env(cls, session_store: SessionStore, *, base_url: Optional[str] = None) -> "GrokClient":
        """Build a client from environment variables only -- handy for tests."""

        base = base_url or os.environ.get("GROK_CHAT_PROXY_BASE_URL", "https://cli-chat-proxy.grok.com")
        version = os.environ.get("GROK_CHAT_PROXY_VERSION", "v1")
        return cls.from_settings(
            settings=Settings(
                grok_chat_proxy_base_url=base,
                grok_chat_proxy_version=version,
            ),
            session_store=session_store,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GrokClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---- header assembly ------------------------------------------------

    async def _assemble_headers(
        self,
        *,
        conv_id: Optional[str] = None,
        req_id: Optional[str] = None,
        model_override: Optional[str] = None,
        user_agent: Optional[str] = None,
        with_traceparent: bool = False,
        conversation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        attach_auth_response_tag: bool = False,
        extra: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """Build the full header bag observed on real upstream traffic.

        All ``x-grok-{agent-id, session-id, conv-id, req-id}`` are emitted
        even when empty, mirroring the official CLI. The auth tag
        (``x-xai-token-auth``) and ``x-authenticateresponse`` are likewise
        always present or absent according to the call site, never absent-by-
        oversight.
        """

        session = await self._session_store.current()
        headers = build_grok_headers(
            client_name=self._settings.grok_client_name,
            client_version=self._settings.grok_client_version,
            client_surface=self._settings.grok_client_surface,
            client_identifier=self._settings.grok_client_identifier,
            agent_id=self._agent_id,
            session_id=session_id if session_id is not None else self._session_id,
            conv_id=conv_id if conv_id is not None else "",
            req_id=req_id or request_id or new_req_id(),
            model_override=model_override,
            user_id=session.user_id,
            token_auth_tag=self._settings.grok_token_auth,
            authenticate_response_tag=(
                self._settings.grok_authenticate_response_tag
                if attach_auth_response_tag
                else None
            ),
            traceparent=new_traceparent() if with_traceparent else None,
            tracestate="" if with_traceparent else None,
            conversation_id_alias=conversation_id,
            request_id_alias=request_id,
            extra=extra,
        )
        headers["authorization"] = session.to_header_value()
        headers["accept"] = "application/json"
        if user_agent:
            headers["user-agent"] = user_agent
        return headers

    def _build_url(self, *parts: str) -> str:
        path = "/".join(p.strip("/") for p in (self._version, *parts) if p)
        return f"/{path}"

    # ---- primary: chat completions ----------------------------------------

    async def health(self) -> Mapping[str, Any]:
        headers = await self._assemble_headers()
        resp = await self._client.get(self._build_url("models"), headers=headers)
        return self._check(resp)

    async def list_models(self) -> Mapping[str, Any]:
        headers = await self._assemble_headers()
        resp = await self._client.get(self._build_url("models"), headers=headers)
        return self._check(resp)

    async def chat(self, body: Mapping[str, Any], *, conv_id: Optional[str] = None) -> Mapping[str, Any]:
        """POST /{v1}/chat/completions -- non-streaming."""

        headers = await self._assemble_headers(conv_id=conv_id, model_override=body.get("model"))
        url = self._build_url("chat/completions")
        resp = await self._client.post(url, headers=headers, json=body)
        return self._check(resp)

    async def stream_chat(
        self,
        body: Mapping[str, Any],
        *,
        conv_id: Optional[str] = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """POST /{v1}/chat/completions with ``stream=true`` (SSE)."""

        headers = await self._assemble_headers(conv_id=conv_id, model_override=body.get("model"))
        headers["accept"] = "text/event-stream"
        url = self._build_url("chat/completions")
        req_id = headers.get("x-grok-req-id")
        log.debug("streaming chat request", extra={"url": url, "req_id": req_id})

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                body_text = await resp.aread()
                raise GrokAPIError(
                    status=resp.status_code,
                    body=body_text.decode("utf-8", "replace"),
                    request_id=req_id,
                )

            async for raw in resp.aiter_lines():
                line = raw.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        return
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("malformed SSE chunk: %r", payload)
                    continue

    # ---- primary: responses API -------------------------------------------

    async def responses(
        self,
        body: Mapping[str, Any],
        *,
        conv_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """POST /{v1}/responses -- non-streaming (Responses API).

        Used by the official CLI for everything that isn't legacy OpenAI
        format. Body must follow the upstream's Responses schema::

            {
                "input": [{"type": "message", "role": ..., "content": ...}, ...],
                "model": "grok-build",
                "max_output_tokens": int,
                "stream": bool,
                "temperature": float,
                "tools": [...],
                "tool_choice": {...} or "auto" / "required" / "none",
                "reasoning": {"summary": "concise"},
                "store": bool,
                "include": [...],
            }
        """

        headers = await self._assemble_headers(
            conv_id=conv_id,
            model_override=body.get("model"),
            user_agent=chat_user_agent(
                self._settings.grok_client_identifier,
                self._settings.grok_client_version,
            ),
            with_traceparent=True,
        )
        url = self._build_url("responses")
        resp = await self._client.post(url, headers=headers, json=body)
        return self._check(resp)

    async def stream_responses(
        self,
        body: Mapping[str, Any],
        *,
        conv_id: Optional[str] = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """POST /{v1}/responses with ``stream=true`` (SSE)."""

        headers = await self._assemble_headers(
            conv_id=conv_id,
            model_override=body.get("model"),
            user_agent=chat_user_agent(
                self._settings.grok_client_identifier,
                self._settings.grok_client_version,
            ),
            with_traceparent=True,
        )
        headers["accept"] = "text/event-stream"
        url = self._build_url("responses")

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                body_text = await resp.aread()
                raise GrokAPIError(
                    status=resp.status_code,
                    body=body_text.decode("utf-8", "replace"),
                    request_id=headers.get("x-grok-req-id"),
                )

            async for raw in resp.aiter_lines():
                line = raw.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        return
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("malformed SSE chunk: %r", payload)
                    continue

    # ---- sideband: settings / user / billing / mcp / feedback ---------------

    async def get_settings(self) -> Mapping[str, Any]:
        """GET /{v1}/settings -- returns ``min_client_version``, ``force_update``, etc."""

        headers = await self._assemble_headers(with_traceparent=False)
        resp = await self._client.get(self._build_url("settings"), headers=headers)
        return self._check(resp)

    async def get_user(self, *, include: str = "subscription") -> Mapping[str, Any]:
        """GET /{v1}/user?include=... -- profile, email, blocking reasons."""

        headers = await self._assemble_headers()
        url = self._build_url("user") + f"?include={include}"
        resp = await self._client.get(url, headers=headers)
        return self._check(resp)

    async def get_billing(self, *, format_: str = "credits") -> Mapping[str, Any]:
        """GET /{v1}/billing?format=credits -- usage / prepaid balance."""

        headers = await self._assemble_headers()
        url = self._build_url("billing") + f"?format={format_}"
        resp = await self._client.get(url, headers=headers)
        return self._check(resp)

    async def get_mcp_configs(self) -> Mapping[str, Any]:
        """GET /{v1}/mcp/configs -- user-configured MCP servers."""

        headers = await self._assemble_headers()
        resp = await self._client.get(self._build_url("mcp/configs"), headers=headers)
        return self._check(resp)

    async def list_mcp_tools(self) -> Mapping[str, Any]:
        """GET /{v1}/mcp/tools/list -- catalog of available MCP tools."""

        headers = await self._assemble_headers()
        resp = await self._client.get(self._build_url("mcp/tools/list"), headers=headers)
        return self._check(resp)

    async def get_feedback_config(self) -> Mapping[str, Any]:
        """GET /{v1}/feedback/config -- A/B feedback sampling config."""

        headers = await self._assemble_headers(with_traceparent=True)
        resp = await self._client.get(self._build_url("feedback/config"), headers=headers)
        return self._check(resp)

    # ---- sideband: sessions lifecycle --------------------------------------

    async def register_session(
        self,
        *,
        session_id: str,
        cwd: str,
        model_id: str,
        device_id: str,
        hostname_value: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """POST /{v1}/sessions/register -- declare session start."""

        payload = {
            "sessionId": session_id,
            "cwd": cwd,
            "gcsTracePrefix": session_id,
            "modelId": model_id,
            "hostname": hostname_value or hostname_of(),
            "deviceId": device_id,
        }
        headers = await self._assemble_headers()
        resp = await self._client.post(self._build_url("sessions/register"), headers=headers, json=payload)
        return self._check(resp)

    async def session_replicas_update(self, session_id: str, summary: str) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/replicas/update -- bulk-update session snapshot."""

        headers = await self._assemble_headers()
        url = f"{self._build_url('sessions', session_id, 'replicas/update')}"
        body = {"summary": summary}
        resp = await self._client.post(url, headers=headers, json=body)
        return self._check(resp)

    async def session_signals(self, session_id: str, signals: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/signals -- aggregate session telemetry."""

        headers = await self._assemble_headers(with_traceparent=True)
        url = f"{self._build_url('sessions', session_id, 'signals')}"
        resp = await self._client.post(url, headers=headers, json=signals)
        return self._check(resp)

    async def session_turn_deltas(self, session_id: str, deltas: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/turn-deltas -- per-turn telemetry."""

        headers = await self._assemble_headers()
        url = f"{self._build_url('sessions', session_id, 'turn-deltas')}"
        resp = await self._client.post(url, headers=headers, json=deltas)
        return self._check(resp)

    # ---- sideband: tracing / log streaming --------------------------------

    async def stream_storage(
        self,
        *,
        storage_path: str,
        ndjson_chunks: Iterable[bytes],
    ) -> Mapping[str, Any]:
        """POST /{v1}/storage -- NDJSON stream of log lines.

        The HAR shows ``content-type: application/x-ndjson`` with header
        ``x-storage-path: auth-diagnostics/<version>/<email>/<id>.jsonl``.
        Used by the official CLI to push aggregated diagnostics back to
        xAI. Body is line-delimited JSON; each line is a single log entry.
        """

        path = storage_path.lstrip("/")
        url = "https://cli-chat-proxy.grok.com/v1/storage"  # absolute to avoid base path mishaps

        async def _iter_body() -> AsyncIterator[bytes]:
            for chunk in ndjson_chunks:
                yield chunk

        headers = await self._assemble_headers(
            extra={"x-storage-path": path},
            user_agent=default_user_agent(
                self._settings.grok_client_identifier,
                self._settings.grok_client_version,
            ),
        )
        resp = await self._client.post(
            url,
            headers={**headers, "content-type": "application/x-ndjson"},
            content=_iter_body(),
        )
        return self._check(resp)

    async def post_traces(self, *, otlp_payload: bytes) -> Mapping[str, Any]:
        """POST /{v1}/traces -- OTLP/protobuf telemetry."""

        headers = await self._assemble_headers(
            extra={
                "user-agent": "OTel-OTLP-Exporter-Rust/0.32.0",
                "content-type": "application/x-protobuf",
            },
        )
        url = self._build_url("traces")
        resp = await self._client.post(url, headers=headers, content=otlp_payload)
        return self._check(resp)

    # ---- error handling --------------------------------------------------

    @staticmethod
    def _check(resp: httpx.Response) -> Mapping[str, Any]:
        if resp.status_code >= 400:
            raise GrokAPIError(
                status=resp.status_code,
                body=resp.text,
                request_id=resp.headers.get("x-grok-req-id"),
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text}

    @asynccontextmanager
    async def new_request_scope(self) -> AsyncIterator[None]:
        """Convenience for callers that want a fresh req_id per logical request."""

        # We don't currently mutate any client state per scope, but keep the
        # hook so future iteration (e.g. fresh headers per turn) has a slot.
        yield None
