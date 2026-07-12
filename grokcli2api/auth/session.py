"""Session value-object and the abstract provider interface.

A :class:`Session` carries everything needed to authenticate an outbound
request to the Grok CLI chain:

* ``token`` -- the bearer token. ``grokservers`` expects this as either a
  bare ``SessionToken`` or a JWT minted through xAI OAuth/OIDC.
* ``expires_at`` -- Unix timestamp. Used to decide when to refresh.
* ``surface`` -- ``cli`` / ``tui`` / ``agent``. Populates the
  ``x-grok-client-surface`` header downstream.

The :class:`SessionStore` is the process-wide façade that
:class:`grokcli2api.grok.client.GrokClient` interacts with. It selects a
provider at construction time and tracks an in-memory copy of the latest session.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from grokcli2api.utils.logger import get_logger

log = get_logger(__name__)


class AuthError(RuntimeError):
    """Raised when no usable token is available or refresh failed."""


@dataclass(frozen=True)
class Session:
    """A single logged-in session. Immutable."""

    token: str
    surface: str = "cli"
    user_id: Optional[str] = None
    obtained_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None  # when the token becomes unusable

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        """Return True if the session expires within ``skew_seconds`` of now.

        Sessions with ``expires_at=None`` are treated as permanently valid.
        The official CLI also falls back to a 30-day lifetime in that case,
        but we want to push the assumption back to the operator -- they may
        intentionally be running with a long-lived SessionToken.
        """
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - skew_seconds)

    def to_header_value(self) -> str:
        return f"Bearer {self.token}"


class AuthProvider(abc.ABC):
    """Strategy interface. Implementations return a fresh :class:`Session`."""

    @abc.abstractmethod
    async def acquire(self) -> Session:
        """Return a session. May involve file IO, network calls, or user prompts."""

    async def refresh(self, current: Session) -> Session:
        """Refresh an existing session.

        Providers that can exchange a refresh token (or re-read an updated
        auth file) should override this. The default raises :class:`AuthError`,
        which tells :class:`SessionStore` to fall back to a fresh
        :meth:`acquire` call.
        """

        raise AuthError(f"{type(self).__name__} does not implement token refresh")


class SessionStore:
    """Holds a current :class:`Session` and refreshes it on demand."""

    def __init__(
        self,
        *,
        provider: AuthProvider,
        background_refresh: bool = True,
    ) -> None:
        self._provider = provider
        self._lock = asyncio.Lock()
        self._session: Optional[Session] = None
        self._background_refresh = background_refresh

    async def _do_acquire(self) -> Session:
        """Acquire a fresh session from the provider without taking the lock."""

        return await self._provider.acquire()

    async def current(self) -> Session:
        """Return the active session, acquiring or refreshing as needed."""

        async with self._lock:
            if self._session is None:
                log.info("acquiring initial session from %s", type(self._provider).__name__)
                self._session = await self._do_acquire()
            elif self._background_refresh and self._session.is_expired():
                log.info("session expired, refreshing")
                self._session = await self._do_acquire()
            return self._session

    async def force_refresh(self) -> Session:
        """Discard the cached session and acquire a brand-new one."""

        async with self._lock:
            log.warning("forcing session refresh")
            new_session = await self._do_acquire()
            self._session = new_session
            return new_session

    async def refresh(self, current: Optional[Session] = None) -> Session:
        """Refresh the current session, falling back to re-acquire.

        The provider's :meth:`AuthProvider.refresh` is tried first so real
        OAuth refresh-token flows can be used when available. If the provider
        does not implement refresh (the default), we fall back to
        :meth:`force_refresh`, which for :class:`AuthFileProvider` means
        re-reading ``auth.json`` -- exactly what we need when the official CLI
        has rotated the bearer token in the background.
        """

        async with self._lock:
            log.warning("refreshing session")
            try:
                new_session = await self._provider.refresh(current)
            except AuthError:
                log.debug("provider.refresh not available, falling back to acquire")
                new_session = await self._do_acquire()
            self._session = new_session
            return new_session

    async def close(self) -> None:
        """Hook for providers that hold network resources (e.g. OAuth client)."""

        closer = getattr(self._provider, "close", None)
        if callable(closer):
            result = closer()
            if asyncio.iscoroutine(result):
                await result
