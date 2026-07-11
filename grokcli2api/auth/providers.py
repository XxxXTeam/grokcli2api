"""Built-in :class:`AuthProvider` implementations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from grokcli2api.auth.session import AuthError, AuthProvider, Session
from grokcli2api.utils.logger import get_logger

log = get_logger(__name__)


# 30 days -- matches the fallback the official CLI uses when the server
# doesn't send an explicit expiry.
_DEFAULT_LIFETIME_SECONDS = 30 * 24 * 3600


@dataclass
class SessionTokenProvider(AuthProvider):
    """A provider that returns a fixed ``SessionToken``.

    The simplest path for operators -- paste a token into the ``.env`` and go.
    No expiry is assumed (the operator manages rotation manually).
    """

    token: str
    surface: str = "cli"

    async def acquire(self) -> Session:
        if not self.token:
            raise AuthError("SessionTokenProvider invoked without a token")
        return Session(
            token=self.token,
            surface=self.surface,
            obtained_at=time.time(),
            expires_at=None,
        )


@dataclass
class AuthFileProvider(AuthProvider):
    """Reads a previously authorized ``auth.json`` produced by the official CLI.

    The official CLI keeps tokens at ``~/.grok/auth.json``. The 2026+ schema
    (what ``grok.exe`` actually writes today) looks like:

    .. code-block:: json

        {
          "https://auth.x.ai::<client_id>": {
            "key": "eyJ0eXAi...",        // ← bearer token (NOT "access_token")
            "auth_mode": "oidc",
            "user_id": "...",
            "expires_at": "2026-07-11T10:33:39.565925300Z",   // ISO 8601 string
            "refresh_token": "..."
          }
        }

    We also accept the older ``{tokens: {access_token: ...}}`` schema because
    it was the layout earlier releases used. The parser drills through both
    and prefers the live OIDC entry when present.
    """

    file_path: Path
    surface: str = "cli"

    def __post_init__(self) -> None:
        # Defensive normalisation -- never let a raw ``str`` slip through and
        # surface as ``AttributeError: 'str' object has no attribute 'exists'``
        # at request time. Coerce once, eagerly.
        if not isinstance(self.file_path, Path):
            self.file_path = Path(self.file_path).expanduser()

    async def acquire(self) -> Session:
        if not self.file_path.exists():
            raise AuthError(f"auth file not found: {self.file_path}")

        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AuthError(f"auth file is not valid JSON: {exc}") from exc

        token = _extract_token(raw)
        user_id = _extract_user_id(raw)
        expires_at = _extract_expires(raw)

        if not token:
            raise AuthError("auth file did not contain a usable token")

        return Session(
            token=token,
            surface=self.surface,
            user_id=user_id,
            obtained_at=time.time(),
            expires_at=expires_at,
        )


@dataclass
class OAuthProvider(AuthProvider):
    """Headless xAI OAuth/OIDC placeholder.

    The official flow lives at ``https://auth.x.ai`` (OAuth) and
    ``https://grok.com/oidc`` (OIDC). Headless flows require either a
    device-code grant or a user copy/paste of a code & verifier pair.
    This stub keeps the wire shape so we can drop a real implementation in
    without changing the rest of the codebase.
    """

    client_id: str
    client_secret: Optional[str] = None
    surface: str = "cli"

    async def acquire(self) -> Session:
        raise AuthError(
            "OAuthProvider requires a manual implementation. "
            "Provide a GROK_SESSION_TOKEN instead, or point GROK_AUTH_FILE at "
            "an auth.json file produced by `grok login`."
        )

    async def close(self) -> None:  # noqa: D401 -- provider has no resources
        return None


# --- helpers ---------------------------------------------------------------


# Field-name variants the parser recognises as the bearer token.
# Ordered: most-specific first. The 2026+ xAI schema stores the access
# token in a field literally called ``key`` (not "access_token") -- when we
# hit ``key`` we trust it. Anything else (e.g. ``refresh_token``, ``id_token``)
# gets a different treatment further down.
_BEARER_FIELD_NAMES: tuple[str, ...] = (
    "key",                # 2026+ xAI OIDC entry, see AuthFileProvider docstring
    "access_token",       # canonical OAuth2 name
    "AccessToken",
    "session_token",      # the official term xAI uses in headers ("SessionToken")
    "SessionToken",
    "bearer",
    "Bearer",
    "id_token",           # OpenID Connect ID token (also usable as bearer for some APIs)
    "IDToken",
    "accessToken",
)


def _looks_like_oidc_entry(node: dict) -> bool:
    """Check whether a dict is a per-issuer OIDC session row.

    The 2026 xAI schema wraps active sessions under a key whose name is
    the issuer URL joined to the client id. The inner object carries an
    auth_mode field set to oidc or oauth. That is our reliable marker.

    Returns True for the live session row, False otherwise.
    """

    mode = node.get("auth_mode")
    return isinstance(mode, str) and mode.lower() in {"oidc", "oauth"}



def _find_oidc_entry(raw: dict) -> Optional[dict]:
    """Locate the live OIDC entry inside the parsed auth.json.

    Returns the first dict value that either has auth_mode set, or whose
    "key" field looks like a JWT (starts with "eyJ"). Returns None when
    no candidate matches.
    """

    if not isinstance(raw, dict):
        return None
    candidates: list[dict] = []
    for value in raw.values():
        if isinstance(value, dict):
            candidates.append(value)

    # Prefer an explicit auth_mode marker.
    for node in candidates:
        if _looks_like_oidc_entry(node):
            return node
    # Fall back to a JWT-shaped ``key`` field.
    for node in candidates:
        token_candidate = node.get("key")
        if isinstance(token_candidate, str) and token_candidate.startswith("eyJ"):
            return node
    return None


def _extract_token(raw: dict) -> Optional[str]:
    """Best-effort token extraction across known schema variants.

    Order of preference:

    1. The 2026+ xAI OIDC layout (``<issuer>::<client>``: {``key``: ...}).
    2. ``tokens.access_token`` etc. legacy wrapping.
    3. Top-level field names anywhere in the tree.
    """

    if not isinstance(raw, dict):
        return None

    # 1. Look for the OIDC entry first -- its `key` is the bearer.
    oidc_entry = _find_oidc_entry(raw)
    if oidc_entry is not None:
        for name in _BEARER_FIELD_NAMES:
            value = oidc_entry.get(name)
            if isinstance(value, str) and value:
                return value

    # 2. Legacy ``tokens`` wrapper.
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        for name in _BEARER_FIELD_NAMES:
            value = tokens.get(name)
            if isinstance(value, str) and value:
                return value

    # 3. Top-level field.
    for name in _BEARER_FIELD_NAMES:
        value = raw.get(name)
        if isinstance(value, str) and value:
            return value

    return None


def _extract_user_id(raw: dict) -> Optional[str]:
    """Extract a user identifier from any of the known layouts."""

    if not isinstance(raw, dict):
        return None
    # 1. OIDC entry has its own user_id.
    oidc_entry = _find_oidc_entry(raw)
    if oidc_entry is not None:
        for name in ("user_id", "userId", "UserId", "sub"):
            value = oidc_entry.get(name)
            if isinstance(value, str) and value:
                return value

    # 2. Legacy top-level / nested.
    for name in ("user_id", "userId", "UserId", "subject"):
        value = raw.get(name)
        if isinstance(value, str) and value:
            return value
    sub = raw.get("sub")
    if isinstance(sub, str):
        return sub
    return None


def _parse_iso_timestamp(value: Any) -> Optional[float]:
    """Parse ISO 8601 / RFC 3339 timestamps into a Unix epoch.

    Returns ``None`` if ``value`` is None / not string-like / unparseable.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Python 3.11+ understands the trailing ``Z`` natively.
    try:
        from datetime import datetime
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_expires(raw: dict) -> Optional[float]:
    """Return a Unix timestamp, falling back to the 30-day default lifetime.

    Handles all three layouts:

    * OIDC entry: ``expires_at`` is an ISO 8601 string.
    * Legacy ``tokens.expires_at`` (number or string).
    * Flat top-level field.
    """

    if not isinstance(raw, dict):
        return time.time() + _DEFAULT_LIFETIME_SECONDS

    candidates: list[Any] = []

    # 1. OIDC entry (2026+ xAI layout).
    oidc_entry = _find_oidc_entry(raw)
    if oidc_entry is not None:
        for name in ("expires_at", "ExpiresAt", "exp", "expiry", "expiration"):
            candidates.append(oidc_entry.get(name))

    # 2. Legacy ``tokens`` wrapper.
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        candidates.append(tokens.get("expires_at"))
        candidates.append(tokens.get("ExpiresAt"))

    # 3. Top-level.
    candidates.append(raw.get("expires_at"))
    candidates.append(raw.get("ExpiresAt"))

    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str):
            iso = _parse_iso_timestamp(candidate)
            if iso is not None:
                return iso
            # Last-ditch: plain numeric string.
            try:
                return float(candidate)
            except ValueError:
                continue

    # No expiry present -- fall back to the 30-day lifetime the official CLI uses.
    return time.time() + _DEFAULT_LIFETIME_SECONDS
