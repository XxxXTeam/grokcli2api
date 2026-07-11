"""Authentication subsystem.

Three input modes:

* :class:`SessionTokenProvider` - paste a token directly (easiest).
* :class:`AuthFileProvider` - read a previously authorized ``~/.grok/auth.json``
  produced by the official Grok CLI.
* :class:`OAuthProvider` - run the OAuth/OIDC flow headlessly (placeholder).

All providers are funneled through :class:`SessionStore`, which keeps a
:class:`Session` value-object and exposes the canonical ``Authorization: Bearer``
header plus refresh logic.
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

__all__ = [
    "AuthError",
    "AuthProvider",
    "Session",
    "SessionStore",
    "SessionTokenProvider",
    "AuthFileProvider",
    "OAuthProvider",
]
