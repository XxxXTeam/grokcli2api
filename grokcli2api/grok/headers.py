"""Build the exact ``x-grok-*`` (and friends) header set the official CLI sends.

This module's constants and helpers are derived from a real HAR capture of
``grok.exe`` running against ``cli-chat-proxy.grok.com``. Each header is
documented with the path that sent it; if you find yourself unsure whether
a header is still required, consult the HAR rather than guessing.

Identity layer (always present, even with empty values):

* ``x-grok-client-name`` - static ``grok-shell``
* ``x-grok-client-version`` - build version, e.g. ``0.2.93``
* ``x-grok-client-identifier`` - build artifact name (``grok-shell``)
* ``x-grok-client-surface`` - client surface (``cli`` / ``tui`` / ``agent``)

Token / auth layer:

* ``x-xai-token-auth`` - tag identifying the auth scheme; the official CLI
  uses the literal ``"xai-grok-cli"``.
* ``x-authenticateresponse`` - tag identifying the response kind for the
  auth flow; the official CLI uses ``"authenticate-response"`` on certain
  probe paths.
* ``authorization`` - ``Bearer <jwt>``, injected by the client layer.

User-context layer (sent on /v1/settings, /v1/billing, /v1/traces):

* ``x-userid`` - principal id (string).
* ``x-email`` - email address (string).
* ``x-teamid`` - team id (UUID-ish).

Session layer (per request; the official CLI always emits them, often as
empty strings, so we mirror that):

* ``x-grok-conversation-id`` (a.k.a. ``x-grok-conv-id``) - empty when fresh.
* ``x-grok-session-id`` - empty when fresh.
* ``x-grok-request-id`` (a.k.a. ``x-grok-req-id``) - empty when fresh.
* ``x-grok-agent-id`` - empty when fresh.
* ``x-grok-model-override`` - populated when caller overrides model.

W3C tracing layer:

* ``traceparent`` - ``00-<32-hex-trace-id>-<16-hex-span-id>-01``.
* ``tracestate`` - often empty string.

Telemetry:

* ``x-grok-deployment-idx-grok-user-id`` - composite, still seen on legacy.
"""

from __future__ import annotations

import secrets
import socket
import uuid
from typing import Mapping, Optional


# Real values observed in the official CLI's HAR capture, 2026-07.
DEFAULT_CLIENT_NAME = "grok-shell"
DEFAULT_CLIENT_IDENTIFIER = "grok-shell"
DEFAULT_CLIENT_SURFACE = "tui"
DEFAULT_CLIENT_VERSION = "0.2.93"
DEFAULT_TOKEN_AUTH_TAG = "xai-grok-cli"
DEFAULT_AUTHENTICATE_RESPONSE_TAG = "authenticate-response"
DEFAULT_PLATFORM = "windows"
DEFAULT_ARCH = "x86_64"


# Backwards-compatible aliases (older versions of this module and the
# README still reference these names).
LEGACY_CLIENT_IDENTIFIER = "xai-grok-cli"
LEGACY_CLIENT_NAME = "xai-grok-cli"


def _new_id() -> str:
    """Generate a 32-character lowercase hex string (UUIDv4 without dashes)."""
    return uuid.uuid4().hex


def new_agent_id() -> str:
    """A conversation-bound agent id (per ``x-grok-agent-id``)."""
    return _new_id()


def new_session_id() -> str:
    """A long-lived session id (per ``x-grok-session-id``)."""
    return _new_id()


def new_session_id_v7() -> str:
    """Session ids look like UUIDv7 in the wild -- ``019f4f92-e912-73e1-…``."""
    try:
        return str(uuid.uuid7())
    except AttributeError:
        return str(uuid.uuid4())


def new_req_id() -> str:
    """Per-request id (per ``x-grok-req-id``)."""
    return secrets.token_hex(16)


def new_conv_id() -> str:
    """Per-conversation id (per ``x-grok-conv-id``)."""
    return _new_id()


def new_traceparent(span_id: Optional[str] = None) -> str:
    """Build a W3C ``traceparent`` header (version ``00``)."""

    trace_id = secrets.token_hex(16)
    span = span_id or secrets.token_hex(8)
    return f"00-{trace_id}-{span}-01"


def default_user_agent(client_identifier: str, client_version: str) -> str:
    """Reconstruct the canonical CLI User-Agent observed in the HAR.

    The pager subprocess emits a compound UA, the chat code-path emits the
    single-component variant. We default to the compound form because that's
    what survives across both seen variants for now.
    """

    parts = [f"{client_identifier}/{client_version}"]
    # The pager is a sibling helper; only the chat codepath drops it.
    # Keep the compound form (matches /v1/storage, /v1/billing, etc.).
    parts.insert(0, f"grok-pager/{client_version}")
    platform = DEFAULT_PLATFORM
    arch = DEFAULT_ARCH
    return f"{parts[0]} {parts[1]} ({platform}; {arch})"


def chat_user_agent(client_identifier: str, client_version: str) -> str:
    """The single-component UA seen on /v1/responses traffic."""

    return f"{client_identifier}/{client_version} ({DEFAULT_PLATFORM}; {DEFAULT_ARCH})"


def build_grok_headers(
    *,
    client_name: str = DEFAULT_CLIENT_NAME,
    client_version: str = DEFAULT_CLIENT_VERSION,
    client_surface: str = DEFAULT_CLIENT_SURFACE,
    client_identifier: str = DEFAULT_CLIENT_IDENTIFIER,
    agent_id: str = "",
    session_id: str = "",
    conv_id: str = "",
    req_id: str = "",
    model_override: Optional[str] = None,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    team_id: Optional[str] = None,
    token_auth_tag: str = DEFAULT_TOKEN_AUTH_TAG,
    authenticate_response_tag: Optional[str] = None,
    traceparent: Optional[str] = None,
    tracestate: Optional[str] = None,
    session_id_alias: Optional[str] = None,
    conversation_id_alias: Optional[str] = None,
    request_id_alias: Optional[str] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return the full header dict for one outbound request.

    The official CLI emits ``x-grok-{conv,req,session,agent}-id`` even when
    the values are empty strings. We honour that behaviour so the upstream
    gate doesn't see an unexpected header *set*.
    """

    headers: dict[str, str] = {
        "x-grok-client-version": client_version,
        "x-grok-client-identifier": client_identifier,
        "x-grok-client-surface": client_surface,
        "x-grok-client-name": client_name,
        "x-xai-token-auth": token_auth_tag,
        # Always emit the per-request id group, even when blank.
        "x-grok-agent-id": agent_id,
        "x-grok-session-id": session_id,
        "x-grok-conv-id": conv_id,
        "x-grok-req-id": req_id,
        # Conventional alternative spellings the upstream sometimes uses.
        "x-grok-conversation-id": conversation_id_alias if conversation_id_alias is not None else conv_id,
        "x-grok-session-id-legacy": session_id_alias if session_id_alias is not None else session_id,
        "x-grok-request-id": request_id_alias if request_id_alias is not None else req_id,
    }

    if authenticate_response_tag:
        headers["x-authenticateresponse"] = authenticate_response_tag
    if user_id:
        headers["x-userid"] = user_id
    if email:
        headers["x-email"] = email
    if team_id:
        headers["x-teamid"] = team_id
    if model_override:
        headers["x-grok-model-override"] = model_override
    if traceparent:
        headers["traceparent"] = traceparent
    if tracestate is not None:
        headers["tracestate"] = tracestate

    if extra:
        for key, value in extra.items():
            if value is not None:
                headers[key] = str(value)

    return headers


def hostname() -> str:
    """Best-effort short hostname for ``/v1/sessions/register``."""

    try:
        return socket.gethostname().split(".", 1)[0] or "unknown"
    except Exception:
        return "unknown"


__all__ = [
    "DEFAULT_CLIENT_NAME",
    "DEFAULT_CLIENT_IDENTIFIER",
    "DEFAULT_CLIENT_SURFACE",
    "DEFAULT_CLIENT_VERSION",
    "DEFAULT_TOKEN_AUTH_TAG",
    "DEFAULT_AUTHENTICATE_RESPONSE_TAG",
    "LEGACY_CLIENT_IDENTIFIER",
    "LEGACY_CLIENT_NAME",
    "new_agent_id",
    "new_session_id",
    "new_session_id_v7",
    "new_req_id",
    "new_conv_id",
    "new_traceparent",
    "default_user_agent",
    "chat_user_agent",
    "build_grok_headers",
    "hostname",
]
