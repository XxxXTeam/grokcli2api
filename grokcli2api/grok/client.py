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
        transport: Optional[httpx.AsyncBaseTransport] = None,
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

        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "http2": http2,
            "proxy": proxy,
            "timeout": httpx.Timeout(timeout_seconds, connect=10.0, read=timeout_seconds, write=30.0),
            "verify": not settings.grok_tls_insecure_skip_verify,
            "follow_redirects": False,
            "headers": {
                "accept-encoding": "gzip, br",
                # default User-Agent echoes the official compound form; some
                # paths override with the single-component chat variant.
                "user-agent": default_user_agent(
                    settings.grok_client_identifier,
                    settings.grok_client_version,
                ),
            },
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

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

    # ---- low-level request helpers with 401 refresh -----------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        assemble: bool = False,
        assemble_kwargs: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        **httpx_kwargs: Any,
    ) -> httpx.Response:
        """Execute a non-streaming request, refreshing auth and retrying once on 401."""

        async def _make() -> httpx.Response:
            if assemble:
                headers = await self._assemble_headers(**dict(assemble_kwargs or {}))
                if extra_headers:
                    headers.update(extra_headers)
                httpx_kwargs["headers"] = headers
            return await self._client.request(method, url, **httpx_kwargs)

        resp = await _make()
        if resp.status_code == 401:
            log.warning("upstream returned 401, refreshing session and retrying once")
            await self._session_store.refresh()
            resp = await _make()
        return resp

    @asynccontextmanager
    async def _stream(
        self,
        method: str,
        url: str,
        *,
        assemble: bool = False,
        assemble_kwargs: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        **httpx_kwargs: Any,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming request, refreshing auth and retrying once on 401."""

        async def _make() -> Any:
            if assemble:
                headers = await self._assemble_headers(**dict(assemble_kwargs or {}))
                if extra_headers:
                    headers.update(extra_headers)
                httpx_kwargs["headers"] = headers
            return self._client.stream(method, url, **httpx_kwargs)

        cm = await _make()
        async with cm as resp:
            if resp.status_code == 401:
                log.warning("upstream returned 401 on stream, refreshing session and retrying once")
                await self._session_store.refresh()
                cm = await _make()
                async with cm as resp:
                    yield resp
                    return
            yield resp

    async def _check_stream_status(
        self,
        resp: httpx.Response,
        req_id: Optional[str] = None,
    ) -> None:
        """Raise :class:`GrokAPIError` for a non-OK streaming response."""

        if resp.status_code >= 400:
            body_text = await resp.aread()
            raise GrokAPIError(
                status=resp.status_code,
                body=body_text.decode("utf-8", "replace"),
                request_id=req_id,
            )

    async def _iter_sse(
        self,
        resp: httpx.Response,
        req_id: Optional[str] = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Yield parsed SSE ``data:`` payloads from a streaming response."""

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

    # ---- primary: chat completions ----------------------------------------

    async def health(self) -> Mapping[str, Any]:
        return self._check(await self._request(
            "GET",
            self._build_url("models"),
            assemble=True,
        ))

    async def list_models(self) -> Mapping[str, Any]:
        return self._check(await self._request(
            "GET",
            self._build_url("models"),
            assemble=True,
        ))

    async def chat(self, body: Mapping[str, Any], *, conv_id: Optional[str] = None) -> Mapping[str, Any]:
        """POST /{v1}/chat/completions -- non-streaming."""

        return self._check(await self._request(
            "POST",
            self._build_url("chat/completions"),
            assemble=True,
            assemble_kwargs={"conv_id": conv_id, "model_override": body.get("model")},
            json=body,
        ))

    async def stream_chat(
        self,
        body: Mapping[str, Any],
        *,
        conv_id: Optional[str] = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """POST /{v1}/chat/completions with ``stream=true`` (SSE)."""

        url = self._build_url("chat/completions")
        log.debug("streaming chat request", extra={"url": url})

        async with self._stream(
            "POST",
            url,
            assemble=True,
            assemble_kwargs={"conv_id": conv_id, "model_override": body.get("model")},
            extra_headers={"accept": "text/event-stream"},
            json=body,
        ) as resp:
            req_id = resp.request.headers.get("x-grok-req-id") if resp.request else None
            await self._check_stream_status(resp, req_id)
            async for chunk in self._iter_sse(resp, req_id):
                yield chunk

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

        return self._check(await self._request(
            "POST",
            self._build_url("responses"),
            assemble=True,
            assemble_kwargs={
                "conv_id": conv_id,
                "model_override": body.get("model"),
                "user_agent": chat_user_agent(
                    self._settings.grok_client_identifier,
                    self._settings.grok_client_version,
                ),
                "with_traceparent": True,
            },
            json=body,
        ))

    async def stream_responses(
        self,
        body: Mapping[str, Any],
        *,
        conv_id: Optional[str] = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """POST /{v1}/responses with ``stream=true`` (SSE)."""

        async with self._stream(
            "POST",
            self._build_url("responses"),
            assemble=True,
            assemble_kwargs={
                "conv_id": conv_id,
                "model_override": body.get("model"),
                "user_agent": chat_user_agent(
                    self._settings.grok_client_identifier,
                    self._settings.grok_client_version,
                ),
                "with_traceparent": True,
            },
            extra_headers={"accept": "text/event-stream"},
            json=body,
        ) as resp:
            req_id = resp.request.headers.get("x-grok-req-id") if resp.request else None
            await self._check_stream_status(resp, req_id)
            async for chunk in self._iter_sse(resp, req_id):
                yield chunk

    # ---- sideband: settings / user / billing / mcp / feedback ---------------

    async def get_settings(self) -> Mapping[str, Any]:
        """GET /{v1}/settings -- returns ``min_client_version``, ``force_update``, etc."""

        return self._check(await self._request(
            "GET",
            self._build_url("settings"),
            assemble=True,
            assemble_kwargs={"with_traceparent": False},
        ))

    async def get_user(self, *, include: str = "subscription") -> Mapping[str, Any]:
        """GET /{v1}/user?include=... -- profile, email, blocking reasons."""

        return self._check(await self._request(
            "GET",
            self._build_url("user") + f"?include={include}",
            assemble=True,
        ))

    async def get_billing(self, *, format_: str = "credits") -> Mapping[str, Any]:
        """GET /{v1}/billing?format=credits -- usage / prepaid balance."""

        return self._check(await self._request(
            "GET",
            self._build_url("billing") + f"?format={format_}",
            assemble=True,
        ))

    async def get_mcp_configs(self) -> Mapping[str, Any]:
        """GET /{v1}/mcp/configs -- user-configured MCP servers."""

        return self._check(await self._request(
            "GET",
            self._build_url("mcp/configs"),
            assemble=True,
        ))

    async def list_mcp_tools(self) -> Mapping[str, Any]:
        """GET /{v1}/mcp/tools/list -- catalog of available MCP tools."""

        return self._check(await self._request(
            "GET",
            self._build_url("mcp/tools/list"),
            assemble=True,
        ))

    async def get_feedback_config(self) -> Mapping[str, Any]:
        """GET /{v1}/feedback/config -- A/B feedback sampling config."""

        return self._check(await self._request(
            "GET",
            self._build_url("feedback/config"),
            assemble=True,
            assemble_kwargs={"with_traceparent": True},
        ))

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
        return self._check(await self._request(
            "POST",
            self._build_url("sessions/register"),
            assemble=True,
            json=payload,
        ))

    async def session_replicas_update(self, session_id: str, summary: str) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/replicas/update -- bulk-update session snapshot."""

        return self._check(await self._request(
            "POST",
            self._build_url("sessions", session_id, "replicas/update"),
            assemble=True,
            json={"summary": summary},
        ))

    async def session_signals(self, session_id: str, signals: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/signals -- aggregate session telemetry."""

        return self._check(await self._request(
            "POST",
            self._build_url("sessions", session_id, "signals"),
            assemble=True,
            assemble_kwargs={"with_traceparent": True},
            json=signals,
        ))

    async def session_turn_deltas(self, session_id: str, deltas: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST /{v1}/sessions/<id>/turn-deltas -- per-turn telemetry."""

        return self._check(await self._request(
            "POST",
            self._build_url("sessions", session_id, "turn-deltas"),
            assemble=True,
            json=deltas,
        ))

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

        return self._check(await self._request(
            "POST",
            url,
            assemble=True,
            assemble_kwargs={
                "extra": {"x-storage-path": path},
                "user_agent": default_user_agent(
                    self._settings.grok_client_identifier,
                    self._settings.grok_client_version,
                ),
            },
            extra_headers={"content-type": "application/x-ndjson"},
            content=_iter_body(),
        ))

    async def post_traces(self, *, otlp_payload: bytes) -> Mapping[str, Any]:
        """POST /{v1}/traces -- OTLP/protobuf telemetry."""

        return self._check(await self._request(
            "POST",
            self._build_url("traces"),
            assemble=True,
            assemble_kwargs={
                "extra": {
                    "user-agent": "OTel-OTLP-Exporter-Rust/0.32.0",
                    "content-type": "application/x-protobuf",
                },
            },
            content=otlp_payload,
        ))

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
