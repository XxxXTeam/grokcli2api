"""Authentication subsystem.

Two layers:

Upstream auth (used by :class:`grokcli2api.grok.client.GrokClient` to authenticate
against ``cli-chat-proxy.grok.com``):

* :class:`SessionTokenProvider` - paste a token directly (easiest).
* :class:`AuthFileProvider` - read a previously authorized ``~/.grok/auth.json``
  produced by the official Grok CLI.
* :class:`OAuthProvider` - run the OAuth/OIDC flow headlessly (placeholder).

All upstream providers are funneled through :class:`SessionStore`, which keeps a
:class:`Session` value-object and exposes the canonical ``Authorization: Bearer``
header plus refresh logic.

Server-side auth (used by the FastAPI dependency :func:`require_api_key` to
gate the local HTTP endpoint):

* Comma-separated ``GROK_API_KEYS`` env var. Empty == gate disabled
  (backward compatible).
"""

from grokcli2api.auth.session import (
    AuthError,
    AuthProvider,
    Session,
    SessionStore,
)
from grokcli2api.auth.providers import (
    AuthFileProvider,
    OAuthProvider,
    SessionTokenProvider,
)
from grokcli2api.auth.api_key import (
    ApiKeyError,
    require_api_key,
)

__all__ = [
    "AuthError",
    "AuthProvider",
    "Session",
    "SessionStore",
    "SessionTokenProvider",
    "AuthFileProvider",
    "OAuthProvider",
    "ApiKeyError",
    "require_api_key",
]
