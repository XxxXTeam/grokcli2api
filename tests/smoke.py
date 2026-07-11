"""Self-contained smoke test that exercises the package end-to-end without
requiring a real ``cli-chat-proxy.grok.com``.

Run with: ``python -m tests.smoke`` (from the repo root).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make the project importable when this file is run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from grokcli2api.auth.providers import SessionTokenProvider  # noqa: E402
from grokcli2api.auth.session import SessionStore  # noqa: E402
from grokcli2api.config import Settings  # noqa: E402
from grokcli2api.grok.client import _build_proxy_arg  # noqa: E402
from grokcli2api.grok.headers import (  # noqa: E402
    build_grok_headers,
    chat_user_agent,
    default_user_agent,
    new_req_id,
)
from grokcli2api.grok.models import list_model_ids  # noqa: E402
from grokcli2api.openai.converter import GrokConverter  # noqa: E402
from grokcli2api.openai.schemas import (  # noqa: E402
    ChatCompletionRequest,
    ChatMessage,
)
from grokcli2api.openai.schemas import make_error  # noqa: E402
from grokcli2api.openai.converter import _upstream_model  # noqa: E402


async def _main() -> int:
    fails: list[str] = []

    # 1. Model catalog is non-empty.
    if not list_model_ids():
        fails.append("list_model_ids() returned []")

    # 2. Settings load with sane defaults.
    settings = Settings()
    if not settings.chat_proxy_base_url:
        fails.append("default chat_proxy_base_url missing")

    # 3. Header builder produces required keys.
    headers = build_grok_headers(
        client_name="xai-grok-cli",
        client_version="0.1.0",
        client_surface="cli",
        client_identifier="xai-grok-cli",
        agent_id="a1",
        session_id="s1",
        conv_id="c1",
        req_id=new_req_id(),
    )
    for required in (
        "x-grok-client-version",
        "x-grok-client-identifier",
        "x-grok-client-surface",
        "x-grok-client-name",
        "x-grok-agent-id",
        "x-grok-session-id",
        "x-grok-conv-id",
        "x-grok-req-id",
    ):
        if required not in headers:
            fails.append(f"missing header: {required}")

    # 4. SessionTokenProvider yields a usable Bearer header.
    store = SessionStore(provider=SessionTokenProvider(token="ey.fake.token"))
    session = await store.current()
    if session.to_header_value() != "Bearer ey.fake.token":
        fails.append("Session header should be 'Bearer ey.fake.token'")

    # 5. Converter round-trips an OpenAI request.
    request = ChatCompletionRequest(
        model="grok-4",
        messages=[
            ChatMessage(role="system", content="You are a friendly assistant."),
            ChatMessage(role="user", content="hi"),
        ],
        temperature=0.2,
        stream=False,
    )
    converter = GrokConverter()
    prepared = converter.prepare_request(request)
    if prepared.get("model") != "grok-4":
        fails.append("prepared model mismatch")
    if prepared.get("temperature") != 0.2:
        fails.append("temperature not forwarded")
    if not isinstance(prepared.get("messages"), list) or len(prepared["messages"]) != 2:
        fails.append("messages were not forwarded")

    # 6. Converter normalises a synthetic upstream chunk.
    raw_chunk = {
        "id": "chatcmpl-abc",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "grok-4",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "hi"},
                "finish_reason": None,
            }
        ],
    }
    normalized = converter.normalize_stream_chunk(raw_chunk, fallback_model="grok-4")
    if normalized["choices"][0]["delta"]["content"] != "hi":
        fails.append("stream delta lost content")

    # 7. Conversion handles tool messages.
    tool_request = ChatCompletionRequest(
        model="grok-4",
        messages=[
            ChatMessage(role="tool", tool_call_id="abc", content='{"x":1}'),
        ],
    )
    tool_prepared = converter.prepare_request(tool_request)
    tool_msg = tool_prepared["messages"][0]
    if tool_msg.get("tool_call_id") != "abc":
        fails.append("tool_call_id not preserved")
    if tool_msg.get("content") != '{"x":1}':
        fails.append("tool content not preserved")

    # 8. Passthrough of extension fields (Grok extras).
    ext_request = ChatCompletionRequest(
        model="grok-code-fast-1",
        messages=[ChatMessage(role="user", content="hi")],
    )
    ext_request.reasoning_effort = "low"  # type: ignore[attr-defined]
    ext_prepared = converter.prepare_request(ext_request)
    if ext_prepared.get("reasoning_effort") != "low":
        fails.append("extension field reasoning_effort not forwarded")

    # 9. Force refresh replaces session in store.
    first = await store.current()
    second = await store.force_refresh()
    if first is second:
        fails.append("force_refresh should produce a new Session instance")
    if second.to_header_value() != "Bearer ey.fake.token":
        fails.append("refreshed session lost token")

    # 10. Proxy: unset → fall through to httpx env-var defaults.
    s_none = Settings(_env_file=None)
    if _build_proxy_arg(s_none) is not None:
        fails.append("no proxy configured → _build_proxy_arg must return None")

    # 11. Proxy: HTTP proxy URL.
    import httpx  # noqa: E402
    s_http = Settings(_env_file=None, grok_proxy_url="http://127.0.0.1:8888")
    proxy_http = _build_proxy_arg(s_http)
    if not isinstance(proxy_http, httpx.Proxy):
        fails.append("HTTP proxy should produce httpx.Proxy")
    elif str(proxy_http.url) != "http://127.0.0.1:8888":
        fails.append(f"proxy URL mismatch: {proxy_http.url!r}")

    # 12. Proxy: SOCKS5 / HTTPS proxies are passed through.
    s_socks = Settings(_env_file=None, grok_proxy_url="socks5://127.0.0.1:1080")
    proxy_socks = _build_proxy_arg(s_socks)
    if not isinstance(proxy_socks, httpx.Proxy) or "socks5" not in str(proxy_socks.url):
        fails.append("SOCKS5 proxy URL not preserved")

    # 13. Proxy: NO_PROXY patterns surfaced via os.environ['NO_PROXY'].
    import os  # noqa: E402
    os.environ.pop("NO_PROXY", None)
    s_excl = Settings(
        _env_file=None,
        grok_proxy_url="http://127.0.0.1:8888",
        grok_no_proxy="localhost, *.internal, 10.0.0.0/8",
    )
    proxy_excl = _build_proxy_arg(s_excl)
    no_proxy_env = os.environ.get("NO_PROXY", "")
    parsed_patterns = [p for p in s_excl.no_proxy_patterns or [] if p and p in no_proxy_env]
    if not isinstance(proxy_excl, httpx.Proxy):
        fails.append("excluded proxy should produce httpx.Proxy")
    elif len(parsed_patterns) != 3:
        fails.append(f"NO_PROXY patterns not propagated to env (got {no_proxy_env!r})")

    # 14. OpenAI-shaped error body must be a single-layer ``{"error": {...}}`` --
    #     the regression that broke Cherry Studio / Zod-validated clients.
    err = make_error("token invalid", type_="auth_error", code="401")
    if set(err.keys()) != {"error"}:
        fails.append(f"error body should have exactly one 'error' key, got {list(err.keys())}")
    inner = err.get("error") or {}
    if not isinstance(inner, dict) or "message" not in inner or "type" not in inner:
        fails.append(f"inner error dict missing required keys, got {inner!r}")
    # And nothing doubly-wrapped, ever.
    import json  # noqa: E402
    err_str = json.dumps(err)
    if err_str.count('"error"') != 1:
        fails.append(f"error body must contain exactly one 'error' key (got {err_str})")

    # 15. AuthFile parsing covers the 2026+ xAI OIDC schema (real ~/.grok/auth.json).
    from grokcli2api.auth.providers import AuthFileProvider, _extract_token, _extract_user_id, _extract_expires  # noqa: E402
    real_oidc = {
        "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828": {
            "key": "eyJabc.real-token",
            "auth_mode": "oidc",
            "user_id": "u-test",
            "email": "x@y.z",
            "refresh_token": "rtok",
            "expires_at": "2026-07-11T10:33:39.565925300Z",
            "oidc_issuer": "https://auth.x.ai",
            "oidc_client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        },
    }
    if _extract_token(real_oidc) != "eyJabc.real-token":
        fails.append("OIDC schema token not picked up")
    if _extract_user_id(real_oidc) != "u-test":
        fails.append("OIDC schema user_id not picked up")
    iso_expires = _extract_expires(real_oidc)
    if not isinstance(iso_expires, float) or iso_expires <= 0:
        fails.append(f"OIDC ISO expires_at not parsed correctly (got {iso_expires!r})")
    # RFC 3339 with explicit timezone vs Z should round-trip the same instant
    if abs(iso_expires - 1783766019.0) > 60:
        fails.append(f"OIDC ISO expires_at should be ~1783766019 (got {iso_expires!r})")

    # 16. Legacy {tokens: {access_token, expires_at}} still works.
    legacy = {"tokens": {"access_token": "legacy.tok", "expires_at": 1735689600}, "user_id": "u-2"}
    if _extract_token(legacy) != "legacy.tok":
        fails.append("legacy schema token broken")
    if _extract_user_id(legacy) != "u-2":
        fails.append("legacy schema user_id broken")
    if _extract_expires(legacy) != 1735689600.0:
        fails.append("legacy schema unix expires_at broken")

    # 17. Missing fields fall back to the 30-day window (rather than crashing).
    import time as _t  # noqa: E402
    fallback = _extract_expires({})
    if not isinstance(fallback, float) or abs(fallback - (_t.time() + 30 * 24 * 3600)) > 10:
        fails.append(f"empty auth.json should fall back to 30d, got {fallback!r}")

    # 18. End-to-end: write a real OIDC-shaped file, AuthFileProvider reads it.
    import tempfile  # noqa: E402

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            import json as _jsonlib  # noqa: E402
            _jsonlib.dump(real_oidc, f)
            tmp_path = f.name

        async def _e2e() -> str:
            store = SessionStore(provider=AuthFileProvider(file_path=tmp_path))
            sess = await store.current()
            return sess.token

        e2e_tok = await _e2e()
        if e2e_tok != "eyJabc.real-token":
            fails.append(f"e2e AuthFile returned wrong token: {e2e_tok!r}")
    except Exception as exc:
        fails.append(f"e2e AuthFile threw: {exc}")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # 19. GrokAPIError parses structured upstream bodies (the 402 case).
    from grokcli2api.grok.client import GrokAPIError  # noqa: E402

    body_402 = json.dumps({
        "code": "personal-team-blocked:spending-limit",
        "error": "You have run out of credits or need a Grok subscription. Add credits at https://grok.com/?_s=usage or upgrade at https://grok.com/supergrok.",
    })
    err402 = GrokAPIError(status=402, body=body_402, request_id="req-1")
    if err402.upstream_code != "personal-team-blocked:spending-limit":
        fails.append("GrokAPIError did not capture upstream code")
    if err402.upstream_message is None or "grok.com" not in err402.upstream_message:
        fails.append("GrokAPIError did not capture upstream message / URL")
    if "code=personal-team-blocked:spending-limit" not in str(err402):
        fails.append("GrokAPIError str() should include the upstream code")

    # 20. _build_upstream_error_dict / _humanize produce single-layer + hint.
    from grokcli2api.server import _build_upstream_error_dict, _humanize  # noqa: E402

    out = _build_upstream_error_dict(err402)
    if set(out.keys()) != {"error"}:
        fails.append(f"upstream error body must wrap once, got {list(out.keys())}")
    inner = out["error"]
    if inner.get("code") != "personal-team-blocked:spending-limit":
        fails.append("upstream error.code lost in transit")
    if "grok.com" not in (inner.get("message") or ""):
        fails.append("upstream error.message missing URL")
    hint = inner.get("hint")
    if not hint or "credits" not in hint:
        fails.append(f"_humanize failed to produce friendly hint, got {hint!r}")

    # 21. _humanize returns None for unknown codes (defensive default).
    if _humanize("totally-unknown-code") is not None:
        fails.append("_humanize should return None for unknown codes")

    # 22. Non-JSON upstream bodies degrade gracefully.
    err_raw = GrokAPIError(status=500, body="some plain text", request_id=None)
    if err_raw.upstream_code is not None:
        fails.append("non-JSON body should leave upstream_code=None")

    # 23. New header surface (HAR-derived) -- every header is emitted, even empty.
    # Use the top-level imports; re-importing here would shadow the symbol
    # with a local-binding error mid-function.
    hdrs = build_grok_headers(
        user_id="u-1", email="x@y.z", team_id="t-1",
        agent_id="a", session_id="s", conv_id="c", req_id="r",
    )
    for required in (
        "x-grok-client-name", "x-grok-client-version", "x-grok-client-identifier",
        "x-grok-client-surface", "x-xai-token-auth",
        "x-grok-agent-id", "x-grok-session-id", "x-grok-conv-id", "x-grok-req-id",
        "x-userid", "x-email", "x-teamid",
    ):
        if required not in hdrs:
            fails.append(f"new header missing: {required}")
    if "grok-pager" not in default_user_agent("grok-shell", "0.2.93"):
        fails.append("default_user_agent should mention grok-pager")
    if not chat_user_agent("grok-shell", "0.2.93").startswith("grok-shell/0.2.93"):
        fails.append("chat_user_agent should be single-component")

    # 24. Model alias mapping for OpenAI <-> upstream ids.
    if _upstream_model("grok-4") != "grok-build":
        fails.append("grok-4 should map to grok-build")
    if _upstream_model("grok-4.5") != "grok-build":
        fails.append("grok-4.5 should map to grok-build")
    if _upstream_model("grok-build") != "grok-build":
        fails.append("grok-build identity should be preserved")
    if _upstream_model(None) != "grok-build":
        fails.append("default model should be grok-build")

    # 25. Responses API body shape (HAR-derived).
    rcv = GrokConverter()
    resp_body = rcv.prepare_responses_body(ChatCompletionRequest(
        model="grok-4",
        messages=[ChatMessage(role="user", content="hello")],
        stream=False,
        tools=[{"type": "function", "function": {"name": "x"}}],
    ))
    if resp_body.get("model") != "grok-build":
        fails.append("responses body should rewrite model to grok-build")
    if not isinstance(resp_body.get("input"), list) or len(resp_body["input"]) != 1:
        fails.append("responses body should have a 1-element input[]")
    if "reasoning.encrypted_content" not in resp_body.get("include", []):
        fails.append("responses body must include reasoning.encrypted_content")
    if resp_body.get("stream") is not False:
        fails.append("responses body stream flag missing")

    # 26. Model catalog includes grok-build.
    if "grok-build" not in list_model_ids():
        fails.append("MODEL_CATALOG should expose grok-build")

    # 27. Settings: build_grok_headers with empty id group stays present.
    empty = build_grok_headers(agent_id="", session_id="", conv_id="", req_id="")
    if empty["x-grok-conv-id"] != "" or empty["x-grok-req-id"] != "":
        fails.append("empty x-grok-* id fields must be emitted as empty string")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"OK -- {len(list_model_ids())} models, headers={len(headers)} keys")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
