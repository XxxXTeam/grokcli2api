"""Server-side API-key gate for the GrokCli2API HTTP endpoint.

Use:

* Leave ``GROK_API_KEYS``/``GROK_API_KEY`` empty (default) for an unauthenticated
  endpoint that anyone in the network can use -- fine for personal/local use.
* Set one or more keys to gate the OpenAI-compatible surface. The official
  OpenAI client SDK already adds ``Authorization: Bearer <api_key>`` to every
  request, so no client-side configuration is required beyond passing the
  same key into ``OpenAI(api_key=...)``.

The gate accepts two header forms so callers from either OpenAI's pattern or
Azure's pattern can reach the server:

* ``Authorization: Bearer <key>``        (OpenAI; preferred)
* ``Api-Key: <key>``  /  ``api-key: <key>``   (Azure; accepted)

The companion :func:`require_api_key` is a FastAPI dependency that the
top-level router wires into every protected route via
``app.include_router(..., dependencies=[Depends(require_api_key)])``.

Comparison is done with :func:`secrets.compare_digest` so the response time
does not leak key length / matching-prefix information.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from grokcli2api.config import Settings
from grokcli2api.utils.logger import get_logger

log = get_logger(__name__)


class ApiKeyError(HTTPException):
    """Raised when the API-key gate rejects a request."""

    def __init__(self, detail: str, *, code: str = "invalid_api_key") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
        self.code = code


def _credentials_from_request(
    authorization: Optional[str],
    api_key_header: Optional[str],
) -> Optional[str]:
    """Return the raw bearer token from either header style, or ``None``."""

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
        # Some clients send a bare token; treat that as the bearer.
        token = authorization.strip()
        if token and " " not in token:
            return token
    if api_key_header:
        return api_key_header.strip()
    return None


def _match(candidate: str, accepted: list[str]) -> bool:
    """Constant-time check against a list of accepted keys."""

    for expected in accepted:
        # ``secrets.compare_digest`` raises TypeError if lengths differ; wrap
        # it so we never raise out of the gate.
        if secrets.compare_digest(candidate, expected):
            return True
    return False


@dataclass
class ApiKeyPrincipal:
    """Lightweight marker attached to ``request.state`` for handlers that need it."""

    token: str
    matched_index: int


def _enforce(
    accepted: list[str],
    *,
    authorization: Optional[str],
    api_key_header: Optional[str],
) -> ApiKeyPrincipal:
    """Verify a request and return its ``ApiKeyPrincipal``. Raises on failure."""

    if not accepted:
        # Gate disabled -- treat every caller as authenticated.
        return ApiKeyPrincipal(token="<unauthenticated>", matched_index=-1)

    token = _credentials_from_request(authorization, api_key_header)
    if not token:
        log.warning("api-key gate: missing Authorization / Api-Key")
        raise ApiKeyError("missing or malformed Authorization header")

    for index, expected in enumerate(accepted):
        if secrets.compare_digest(token, expected):
            return ApiKeyPrincipal(token=token, matched_index=index)

    log.warning("api-key gate: invalid key")
    raise ApiKeyError("invalid API key")


async def require_api_key(
    request: Request,
    settings: Settings,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    api_key_header: Optional[str] = Header(default=None, alias="Api-Key"),
) -> Optional[ApiKeyPrincipal]:
    """FastAPI dependency: validate the caller against ``GROK_API_KEYS``.

    Returns ``None`` when the gate is disabled (no keys configured) so
    handlers can stay agnostic. Sets ``request.state.principal`` for any
    downstream observer that wants to know which key was used.
    """

    principal = _enforce(
        accepted=settings.api_keys,
        authorization=authorization,
        api_key_header=api_key_header,
    )
    request.state.principal = principal
    return principal


# Pre-built closures the router can drop into ``include_router(dependencies=...)``
# without needing access to ``Settings`` at module top-level (which would create
# import-cycle pain when settings.py imports from extensions).
def api_key_dependency():
    """Return a closure that takes ``Settings`` and returns the dependency.

    Example::

        app.include_router(api_router, dependencies=[Depends(api_key_dependency())])

    FastAPI resolves ``Depends`` callbacks at startup, so this is safe to call
    in factory mode.
    """

    async def _dep(
        request: Request,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
        api_key_header: Optional[str] = Header(default=None, alias="Api-Key"),
    ) -> Optional[ApiKeyPrincipal]:
        settings: Settings = request.app.state.settings
        return await require_api_key(
            request=request,
            settings=settings,
            authorization=authorization,
            api_key_header=api_key_header,
        )

    return _dep


__all__ = [
    "ApiKeyError",
    "ApiKeyPrincipal",
    "api_key_dependency",
    "require_api_key",
]
