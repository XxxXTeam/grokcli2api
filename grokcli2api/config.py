"""Process-wide settings. All values can be overridden by env vars or `.env`."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """GrokCli2API runtime configuration.

    Variable naming: ``GROK2API_*`` for server-side concerns, ``GROK_*`` for things
    that mirror what the official CLI itself uses (``GROK_CLIENT_*`` etc.).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ---- Server ----
    grok2api_host: str = Field(default="0.0.0.0", alias="GROK2API_HOST")
    grok2api_port: int = Field(default=8088, alias="GROK2API_PORT")
    grok2api_log_level: str = Field(default="INFO", alias="GROK2API_LOG_LEVEL")
    app_version: str = "0.1.0"

    # ---- Upstream ----
    # What the official Grok CLI itself talks to. Override only if you have a proxy.
    grok_chat_proxy_base_url: str = Field(
        default="https://cli-chat-proxy.grok.com",
        alias="GROK_CHAT_PROXY_BASE_URL",
    )
    grok_chat_proxy_version: str = Field(default="v1", alias="GROK_CHAT_PROXY_VERSION")

    # ---- Auth (one of the three is required) ----
    grok_session_token: Optional[str] = Field(default=None, alias="GROK_SESSION_TOKEN")
    grok_auth_file: Optional[str] = Field(default=None, alias="GROK_AUTH_FILE")
    grok_oauth_client_id: Optional[str] = Field(default=None, alias="GROK_OAUTH_CLIENT_ID")
    grok_oauth_client_secret: Optional[str] = Field(default=None, alias="GROK_OAUTH_CLIENT_SECRET")

    # ---- Client identity (mirrors what grok.exe sends) ----
    # Default identity comes from a real HAR capture of ``grok.exe`` running
    # against ``cli-chat-proxy.grok.com`` (2026-07). The official CLI ships
    # itself as ``grok-shell`` (NOT ``xai-grok-cli``); the token-auth tag is
    # the literal ``xai-grok-cli`` (a separate field). Auth.json carries
    # ``referrer: grok-build``, which is why some surfaces see the model id
    # ``grok-build`` even though it routes to grok-4.5 internally.
    grok_client_name: str = Field(default="grok-shell", alias="GROK_CLIENT_NAME")
    grok_client_version: str = Field(default="0.2.93", alias="GROK_CLIENT_VERSION")
    grok_client_surface: str = Field(default="tui", alias="GROK_CLIENT_SURFACE")
    grok_client_identifier: str = Field(default="grok-shell", alias="GROK_CLIENT_IDENTIFIER")
    grok_token_auth: str = Field(default="xai-grok-cli", alias="GROK_TOKEN_AUTH")
    grok_authenticate_response_tag: str = Field(
        default="authenticate-response", alias="GROK_AUTHENTICATE_RESPONSE_TAG"
    )

    # ---- TLS / proxy ----
    grok_tls_insecure_skip_verify: bool = Field(default=False, alias="GROK_TLS_INSECURE_SKIP_VERIFY")

    # ---- Outbound proxy ----
    # Single canonical proxy URL applied to all connections routed to the
    # upstream ``cli-chat-proxy.grok.com`` endpoints. Supports the standard
    # schemes recognised by httpx:
    #
    # * ``http://[user:pass@]host:port``         forward HTTP proxy
    # * ``https://[user:pass@]host:port``        CONNECT-over-HTTPS proxy
    # * ``socks5://[user:pass@]host:port``       SOCKS5 (requires ``httpx[socks]``)
    #
    # Set to empty / leave unset to disable explicit proxying. httpx will
    # still honour the standard ``HTTP_PROXY`` / ``HTTPS_PROXY`` /
    # ``ALL_PROXY`` env vars in that case.
    grok_proxy_url: Optional[str] = Field(default=None, alias="GROK_PROXY_URL")

    # Comma-separated hostnames / patterns that must bypass the proxy even
    # when ``grok_proxy_url`` is set. Parsed by httpx via its
    # ``httpx.Proxy(excludes=...)`` machinery.
    grok_no_proxy: Optional[str] = Field(default=None, alias="GROK_NO_PROXY")

    # Derived aliases for ergonomic accessors
    @property
    def host(self) -> str:
        return self.grok2api_host

    @property
    def port(self) -> int:
        return self.grok2api_port

    @property
    def log_level(self) -> str:
        return self.grok2api_log_level.upper()

    @property
    def chat_proxy_base_url(self) -> str:
        return self.grok_chat_proxy_base_url.rstrip("/")

    @property
    def session_token(self) -> Optional[str]:
        return self.grok_session_token

    @property
    def auth_file(self) -> Optional[Path]:
        """Resolve ``GROK_AUTH_FILE`` to an absolute :class:`Path`.

        Returning a ``Path`` (rather than a string) lets :class:`AuthFileProvider`
        use ``.exists()`` / ``.read_text()`` without re-wrapping the value
        and avoids the kind of ``AttributeError`` seen in the wild.
        """

        if not self.grok_auth_file:
            return None
        return Path(self.grok_auth_file).expanduser()

    @property
    def no_proxy_patterns(self) -> Optional[list[str]]:
        """Return a list of NO_PROXY patterns parsed from the comma-separated string.

        Empty / unset returns ``None`` so callers can pass the value straight
        through to httpx without further checks.
        """

        if not self.grok_no_proxy:
            return None
        return [p.strip() for p in self.grok_no_proxy.split(",") if p.strip()]

    def describe(self) -> str:
        proxy = self.grok_proxy_url or "env(default)"
        return (
            f"Settings(host={self.host!r}, port={self.port}, "
            f"chat_proxy={self.chat_proxy_base_url!r}, "
            f"auth={'token' if self.session_token else 'file' if self.auth_file else 'none'}, "
            f"proxy={proxy!r})"
        )
