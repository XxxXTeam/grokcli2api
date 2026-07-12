"""Tests for automatic token refresh on HTTP 401.

These tests use ``httpx.MockTransport`` so they don't need real network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from grokcli2api.auth.providers import AuthFileProvider, SessionTokenProvider  # noqa: E402
from grokcli2api.auth.session import AuthError, AuthProvider, Session, SessionStore  # noqa: E402
from grokcli2api.config import Settings  # noqa: E402
from grokcli2api.grok.client import GrokAPIError, GrokClient  # noqa: E402


def _settings() -> Settings:
    return Settings(_env_file=None, grok_session_token="old-token")


class _RotatingTokenProvider(AuthProvider):
    """Yields a new token on every acquire/refresh call."""

    def __init__(self, tokens: list[str], implement_refresh: bool = True) -> None:
        self._tokens = list(tokens)
        self._index = 0
        self.refresh_calls: list[Optional[Session]] = []
        self._implement_refresh = implement_refresh

    async def acquire(self) -> Session:
        if self._index >= len(self._tokens):
            raise AuthError("out of tokens")
        token = self._tokens[self._index]
        self._index += 1
        return Session(token=token)

    async def refresh(self, current: Session) -> Session:
        self.refresh_calls.append(current)
        if not self._implement_refresh:
            raise AuthError("refresh not implemented")
        return await self.acquire()


class _Auth401Transport(httpx.AsyncBaseTransport):
    """Returns 401 on the first request, then 200 with a JSON body."""

    def __init__(self, ok_body: dict[str, Any]) -> None:
        self.ok_body = ok_body
        self.requests: list[httpx.Request] = []
        self._count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._count += 1
        if self._count == 1:
            return httpx.Response(
                401,
                request=request,
                json={"code": "unauthenticated", "error": "token expired"},
            )
        return httpx.Response(200, request=request, json=self.ok_body)


class _Auth401ForeverTransport(httpx.AsyncBaseTransport):
    """Always returns 401 to verify we don't loop."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.request_count += 1
        return httpx.Response(
            401,
            request=request,
            json={"code": "unauthenticated", "error": "still expired"},
        )


class _SSE401Transport(httpx.AsyncBaseTransport):
    """401 on first stream, then SSE chunks on the second."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.requests: list[httpx.Request] = []
        self._count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._count += 1
        if self._count == 1:
            return httpx.Response(
                401,
                request=request,
                json={"code": "unauthenticated", "error": "token expired"},
            )
        body = "".join(f"data: {c}\n\n" for c in self.chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )


@pytest.mark.asyncio
async def test_non_streaming_retry_on_401() -> None:
    transport = _Auth401Transport({"id": "chatcmpl-ok", "choices": []})
    provider = _RotatingTokenProvider(["old-token", "new-token"])
    store = SessionStore(provider=provider)
    client = GrokClient(
        base_url="https://cli-chat-proxy.grok.com",
        version="v1",
        settings=_settings(),
        session_store=store,
        transport=transport,
    )

    result = await client.chat({"model": "grok-build", "messages": []})

    assert result["id"] == "chatcmpl-ok"
    assert len(transport.requests) == 2
    assert transport.requests[0].headers["authorization"] == "Bearer old-token"
    assert transport.requests[1].headers["authorization"] == "Bearer new-token"
    assert len(provider.refresh_calls) == 1


@pytest.mark.asyncio
async def test_non_streaming_gives_up_after_second_401() -> None:
    transport = _Auth401ForeverTransport()
    provider = _RotatingTokenProvider(["token-a", "token-b"])
    store = SessionStore(provider=provider)
    client = GrokClient(
        base_url="https://cli-chat-proxy.grok.com",
        version="v1",
        settings=_settings(),
        session_store=store,
        transport=transport,
    )

    with pytest.raises(GrokAPIError) as exc_info:
        await client.chat({"model": "grok-build", "messages": []})

    assert exc_info.value.status == 401
    assert transport.request_count == 2


@pytest.mark.asyncio
async def test_streaming_retry_on_401() -> None:
    transport = _SSE401Transport(['{"id":"chunk-1"}'])
    provider = _RotatingTokenProvider(["old-token", "new-token"])
    store = SessionStore(provider=provider)
    client = GrokClient(
        base_url="https://cli-chat-proxy.grok.com",
        version="v1",
        settings=_settings(),
        session_store=store,
        transport=transport,
    )

    chunks = []
    async for chunk in client.stream_chat({"model": "grok-build", "messages": [], "stream": True}):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0]["id"] == "chunk-1"
    assert len(transport.requests) == 2
    assert transport.requests[0].headers["authorization"] == "Bearer old-token"
    assert transport.requests[1].headers["authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_session_store_refresh_falls_back_to_acquire() -> None:
    """Providers that don't implement refresh() are re-acquired on refresh()."""

    provider = _RotatingTokenProvider(["first", "second"], implement_refresh=False)
    store = SessionStore(provider=provider)

    first = await store.current()
    second = await store.refresh(first)

    assert first.token == "first"
    assert second.token == "second"
    # refresh() raised AuthError, so SessionStore fell back to acquire().
    assert len(provider.refresh_calls) == 1


@pytest.mark.asyncio
async def test_auth_file_provider_re_reads_on_401(tmp_path: Path) -> None:
    """When the official CLI updates auth.json, re-reading it yields the new token."""

    auth_path = tmp_path / "auth.json"
    initial = {
        "https://auth.x.ai::client-id": {
            "key": "old-file-token",
            "auth_mode": "oidc",
            "expires_at": "2099-01-01T00:00:00Z",
        }
    }
    auth_path.write_text(json.dumps(initial), encoding="utf-8")

    transport = _Auth401Transport({"id": "ok", "choices": []})
    store = SessionStore(provider=AuthFileProvider(file_path=auth_path))
    client = GrokClient(
        base_url="https://cli-chat-proxy.grok.com",
        version="v1",
        settings=_settings(),
        session_store=store,
        transport=transport,
    )

    # Populate the in-memory session so the first outbound request uses the
    # old token. Then simulate the official CLI refreshing auth.json before
    # the 401 retry happens.
    first_session = await store.current()
    assert first_session.token == "old-file-token"

    updated = {
        "https://auth.x.ai::client-id": {
            "key": "new-file-token",
            "auth_mode": "oidc",
            "expires_at": "2099-01-01T00:00:00Z",
        }
    }
    auth_path.write_text(json.dumps(updated), encoding="utf-8")

    await client.list_models()

    assert len(transport.requests) == 2
    assert transport.requests[0].headers["authorization"] == "Bearer old-file-token"
    assert transport.requests[1].headers["authorization"] == "Bearer new-file-token"


@pytest.mark.asyncio
async def test_session_token_provider_cannot_refresh() -> None:
    """A fixed SessionToken has no refresh path; 401 is surfaced after one retry."""

    transport = _Auth401ForeverTransport()
    store = SessionStore(provider=SessionTokenProvider(token="fixed-token"))
    client = GrokClient(
        base_url="https://cli-chat-proxy.grok.com",
        version="v1",
        settings=_settings(),
        session_store=store,
        transport=transport,
    )

    with pytest.raises(GrokAPIError) as exc_info:
        await client.list_models()

    assert exc_info.value.status == 401
    assert transport.request_count == 2
    # Both attempts used the same fixed token.
    assert transport.requests[0].headers["authorization"] == "Bearer fixed-token"
    assert transport.requests[1].headers["authorization"] == "Bearer fixed-token"
