"""FastAPI surface -- exposes OpenAI-compatible routes on top of :class:`GrokClient`.

Each handler uses :class:`GrokConverter` to translate, then talks to the
underlying :class:`GrokClient`. Streaming responses are emitted with the
proper ``text/event-stream`` content-type and ``[DONE]`` terminator so any
OpenAI SDK (Python / JS / curl / LangChain / etc.) accepts them unchanged.

A second layer of access control sits in front of the OpenAI surface:
whenever ``GROK_API_KEYS`` is non-empty in the active :class:`Settings`,
the protected routes require ``Authorization: Bearer <key>`` (or Azure-style
``Api-Key:``) and reject with ``401`` otherwise. The dependency only fires
when keys are configured, so leaving the env var unset preserves the old
behaviour of an open endpoint.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from grokcli2api.auth.api_key import (
    api_key_dependency,
)
from grokcli2api.auth.providers import (
    AuthFileProvider,
    OAuthProvider,
    SessionTokenProvider,
)
from grokcli2api.auth.session import AuthError, AuthProvider, SessionStore
from grokcli2api.config import Settings
from grokcli2api.grok.client import GrokAPIError, GrokClient
from grokcli2api.grok.headers import new_conv_id
from grokcli2api.grok.models import MODEL_CATALOG
from grokcli2api.openai.converter import GrokConverter
from grokcli2api.openai.schemas import (
    ChatCompletionRequest,
    ErrorPayload,
    ErrorResponse,
    Model,
    ModelList,
    make_error,
)
from grokcli2api.utils.logger import get_logger, silence_http_logs

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Auth wiring
# ---------------------------------------------------------------------------


class _NoopAuthProvider(AuthProvider):
    """Auth provider that always raises -- used when no creds are configured."""

    async def acquire(self) -> None:  # type: ignore[override]
        raise AuthError(
            "no auth configured. Provide GROK_SESSION_TOKEN, "
            "GROK_AUTH_FILE, or GROK_OAUTH_CLIENT_ID."
        )


def _build_session_store(settings: Settings) -> SessionStore:
    """Pick the right auth provider based on configuration."""

    if settings.grok_session_token:
        log.info("auth: using GROK_SESSION_TOKEN")
        return SessionStore(
            provider=SessionTokenProvider(token=settings.grok_session_token)
        )

    if settings.grok_auth_file:
        log.info("auth: using GROK_AUTH_FILE at %s", settings.grok_auth_file)
        return SessionStore(
            provider=AuthFileProvider(file_path=settings.grok_auth_file)
        )

    if settings.grok_oauth_client_id:
        log.info("auth: OAuth client configured (interactive flow stub)")
        return SessionStore(
            provider=OAuthProvider(
                client_id=settings.grok_oauth_client_id,
                client_secret=settings.grok_oauth_client_secret,
            )
        )

    log.warning(
        "no auth configured -- chat completions will reject. "
        "Set GROK_SESSION_TOKEN / GROK_AUTH_FILE / GROK_OAUTH_CLIENT_ID."
    )
    return SessionStore(provider=_NoopAuthProvider())


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise shared state on startup, dispose on shutdown."""

    silence_http_logs()

    settings: Settings = app.state.settings
    session_store = _build_session_store(settings)
    grok_client = GrokClient.from_settings(settings, session_store)

    app.state.session_store = session_store
    app.state.grok_client = grok_client
    app.state.converter = GrokConverter()

    log.info("ready -- %s", settings.describe())
    try:
        yield
    finally:
        log.info("shutting down GrokClient")
        await grok_client.close()
        await session_store.close()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build a fresh :class:`FastAPI` instance, optionally with custom settings."""

    settings = settings or Settings()
    app = FastAPI(
        title="GrokCli2API",
        description=(
            "OpenAI-compatible HTTP API backed by the reverse-engineered "
            "Grok CLI chain (cli-chat-proxy.grok.com)."
        ),
        version=settings.app_version,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    _register_routes(app)
    # Mount the API-key-guarded router last so its dependencies win over
    # anything mounted earlier with the same path.
    app.include_router(_protected_router())
    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _protected_router() -> APIRouter:
    """Build a sub-router that requires ``Authorization: Bearer <key>`` if
    ``GROK_API_KEYS`` is configured.

    Routes mounted on this router are completely unaffected when the gate is
    disabled (the dependency becomes a no-op), so this is a no-op for
    existing deployments that don't set ``GROK_API_KEYS``.
    """

    router = APIRouter(dependencies=[Depends(api_key_dependency())])
    _register_protected_routes(router)
    return router


def _register_routes(app: FastAPI) -> None:
    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": "grokcli2api",
            "version": app.state.settings.app_version,
            "docs": "/docs",
            "openai_compat_endpoints": [
                "/v1/chat/completions",
                "/v1/models",
                "/v1/health",
            ],
        }

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        try:
            payload = await app.state.grok_client.health()
        except GrokAPIError as exc:
            return JSONResponse(
                status_code=200,
                content={"status": "degraded", "upstream_status": exc.status},
            )
        except AuthError:
            return JSONResponse(
                status_code=200,
                content={"status": "no_auth"},
            )
        return JSONResponse({"status": "ok", "upstream": payload})

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        items = [Model(id=m.id).model_dump() for m in MODEL_CATALOG]
        return ModelList(data=items).model_dump()

    @app.get("/v1/models/{model_id}")
    async def model_detail(model_id: str) -> dict[str, Any]:
        for m in MODEL_CATALOG:
            if m.id == model_id:
                return Model(id=m.id).model_dump()
        raise HTTPException(status_code=404, detail=f"unknown model: {model_id}")

    # ---- auth management ------------------------------------------------

    @app.get("/v1/auth/api-key")
    async def auth_api_key_status() -> dict[str, Any]:
        """Tell the operator whether the local API-key gate is on or off.

        Reachable without a key -- this is the introspection path. Do not
        expose the actual key material, just whether the gate is enabled and
        how many keys are configured.
        """

        settings: Settings = app.state.settings
        return {
            "enabled": settings.is_api_key_gate_enabled,
            "key_count": len(settings.api_keys),
        }

    # Everything below is registered on a separate router that carries the
    # ``require_api_key`` dependency -- ``mount_protected_router`` wires it
    # in below.
    pass


def _register_protected_routes(router: APIRouter) -> None:
    """Register chat + sideband routes. All routes in this router get the
    ``require_api_key`` dependency attached at router level.
    """

    @router.post("/v1/auth/refresh")
    async def auth_refresh(request: Request) -> dict[str, Any]:
        session = await request.app.state.session_store.force_refresh()
        return {
            "expires_at": session.expires_at,
            "obtained_at": session.obtained_at,
            "surface": session.surface,
            "user_id": session.user_id,
        }

    @router.get("/v1/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        session = await request.app.state.session_store.current()
        return {
            "surface": session.surface,
            "user_id": session.user_id,
            "expired": session.is_expired(),
            "expires_at": session.expires_at,
        }

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request, body: ChatCompletionRequest) -> Any:
        converter: GrokConverter = request.app.state.converter
        grok: GrokClient = request.app.state.grok_client

        wire_body = converter.prepare_request(body)
        fallback_model = body.model or "grok-build"
        conv_id = body.user or new_conv_id()

        log.debug(
            "chat: model=%s stream=%s msgs=%d tools=%s",
            fallback_model,
            bool(wire_body.get("stream")),
            len(wire_body.get("messages", [])),
            "yes" if wire_body.get("tools") else "no",
        )

        if not body.stream:
            try:
                upstream = await grok.chat(wire_body, conv_id=conv_id)
            except GrokAPIError as exc:
                return _grok_error_response(exc)
            except AuthError as exc:
                return _auth_error_response(exc)
            return converter.parse_non_stream(upstream, fallback_model=fallback_model)

        async def _stream() -> AsyncIterator[bytes]:
            try:
                leading = {
                    "id": "",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": fallback_model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                }
                yield _sse_chunk(leading)

                async for chunk in grok.stream_chat(wire_body, conv_id=conv_id):
                    normalized = converter.normalize_stream_chunk(
                        chunk, fallback_model=fallback_model
                    )
                    yield _sse_chunk(normalized)
                yield b"data: [DONE]\n\n"
            except GrokAPIError as exc:
                _log_upstream_error(exc)
                yield _sse_chunk(_build_upstream_error_dict(exc))
                yield b"data: [DONE]\n\n"
            except AuthError as exc:
                yield _sse_chunk(make_error(str(exc), type_="auth_error", code="401"))
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/v1/responses")
    async def responses_endpoint(request: Request, body: ChatCompletionRequest) -> Any:
        converter: GrokConverter = request.app.state.converter
        grok: GrokClient = request.app.state.grok_client

        wire_body = converter.prepare_responses_body(body)
        fallback_model = body.model or "grok-build"
        conv_id = body.user or new_conv_id()

        log.debug(
            "responses: model=%s stream=%s msgs=%d tools=%s",
            fallback_model,
            bool(wire_body.get("stream")),
            len(wire_body.get("input", [])),
            "yes" if wire_body.get("tools") else "no",
        )

        if not body.stream:
            try:
                upstream = await grok.responses(wire_body, conv_id=conv_id)
            except GrokAPIError as exc:
                return _grok_error_response(exc)
            except AuthError as exc:
                return _auth_error_response(exc)
            return converter.parse_non_stream(
                {**upstream, "model": upstream.get("model") or fallback_model},
                fallback_model=fallback_model,
            )

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in grok.stream_responses(wire_body, conv_id=conv_id):
                    normalized = converter.normalize_stream_chunk(
                        chunk, fallback_model=fallback_model
                    )
                    yield _sse_chunk(normalized)
                yield b"data: [DONE]\n\n"
            except GrokAPIError as exc:
                _log_upstream_error(exc)
                yield _sse_chunk(_build_upstream_error_dict(exc))
                yield b"data: [DONE]\n\n"
            except AuthError as exc:
                yield _sse_chunk(make_error(str(exc), type_="auth_error", code="401"))
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/v1/grok/settings")
    async def grok_settings(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.get_settings())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]

    @router.get("/v1/grok/user")
    async def grok_user(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.get_user())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]

    @router.get("/v1/grok/billing")
    async def grok_billing(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.get_billing())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]

    @router.get("/v1/grok/mcp/configs")
    async def grok_mcp_configs(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.get_mcp_configs())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]

    @router.get("/v1/grok/mcp/tools/list")
    async def grok_mcp_tools_list(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.list_mcp_tools())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]

    @router.get("/v1/grok/feedback/config")
    async def grok_feedback_config(request: Request) -> dict[str, Any]:
        try:
            return dict(await request.app.state.grok_client.get_feedback_config())
        except GrokAPIError as exc:
            return _grok_error_response(exc).body  # type: ignore[union-attr]
        except AuthError as exc:
            return _auth_error_response(exc).body  # type: ignore[union-attr]





# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _sse_chunk(payload: dict[str, Any]) -> bytes:
    """Serialise a dict as one ``data: ...\\n\\n`` SSE frame."""

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _humanize(code: Optional[str]) -> Optional[str]:
    """Map a few well-known upstream codes to short, actionable diagnostics.

    The upstream returns machine-readable codes like
    ``personal-team-blocked:spending-limit`` that are great for telemetry but
    opaque to humans. Return a friendly one-liner with the URL the user
    actually needs.
    """

    if not code:
        return None
    if code == "personal-team-blocked:spending-limit":
        return (
            "your Grok account hit the spending limit. "
            "Add credits at https://grok.com/?_s=usage or upgrade to SuperGrok "
            "at https://grok.com/supergrok."
        )
    return None


def _build_upstream_error_dict(exc: GrokAPIError) -> dict[str, Any]:
    """Build an OpenAI-shaped single-layer ``{"error": {...}}`` body.

    The body's ``code`` field carries the upstream machine-readable code
    (``personal-team-blocked:spending-limit`` etc.) when we recognised one,
    falling back to ``str(status)``. The ``message`` is the upstream's own
    text -- verbatim including URLs the user needs to click.
    """

    error_type = "upstream_error" if exc.status >= 500 else "invalid_request_error"
    upstream_code = exc.upstream_code or str(exc.status)
    friendly = _humanize(exc.upstream_code)

    # Build manually so we don't reshape fields the caller may rely on.
    body = {
        "message": exc.upstream_message or str(exc),
        "type": error_type,
        "code": upstream_code,
        "param": None,
    }
    if friendly:
        body["hint"] = friendly
    return ErrorResponse(error=ErrorPayload(**body)).model_dump(exclude_none=True)


def _log_upstream_error(exc: GrokAPIError) -> None:
    """Log the upstream failure with enough context for triage."""

    friendly = _humanize(exc.upstream_code)
    log.warning(
        "upstream grok %s%s -- %s",
        exc.status,
        f" code={exc.upstream_code}" if exc.upstream_code else "",
        (exc.upstream_message or str(exc))[:400],
    )
    if friendly:
        log.info("hint: %s", friendly)


def _grok_error_response(exc: GrokAPIError) -> JSONResponse:
    """Format an upstream Grok error as an OpenAI-shaped JSON response.

    We mirror the upstream status when it's in the 4xx/5xx range (so 402s
    surface as 402 to the client, not 502), and we always pass the
    upstream ``code`` + raw ``error`` text through so callers can act on it.
    """

    _log_upstream_error(exc)
    status = exc.status if 400 <= exc.status < 600 else 502
    payload = _build_upstream_error_dict(exc)
    return JSONResponse(status_code=status, content=payload)


def _auth_error_response(exc: AuthError) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorPayload(message=str(exc), type="auth_error", code="401")
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=401, content=payload)


# Module-level app for `uvicorn grokcli2api.server:app`.
app = create_app()


__all__ = ["create_app", "app"]
