# GrokCli2API

Reverse-engineered **Grok CLI** client chain, exposed behind an
**OpenAI-compatible HTTP API**.

This project is the natural extension of [`ANALYZE_REPORT.md`](./ANALYZE_REPORT.md):
it implements in Python what `grok.exe` does in Rust, so you can plug any
OpenAI-compatible client (the official `openai` SDK, LangChain, `llama.cpp`
bridges, custom UIs…) in front of the Grok chat proxy without booting the
official binary.

```
┌───────────────┐    HTTPS     ┌─────────────┐    HTTPS    ┌──────────────────────────┐
│ OpenAI client │  ──────────► │ GrokCli2API │  ─────────► │ cli-chat-proxy.grok.com  │
└───────────────┘  /v1/...     └─────────────┘  x-grok-*   └──────────────────────────┘
                                                Authorization: Bearer ...
                                                (replicates the exact header
                                                 decoration of grok.exe)
```

## Features

| Surface                  | Status                                                       |
| ------------------------ | ------------------------------------------------------------ |
| `POST /v1/chat/completions` (stream + non-stream) | ✅ Full OpenAI shape |
| `GET /v1/models`         | ✅ Static catalog mirroring `cli-chat-proxy.grok.com`         |
| `POST /v1/auth/refresh`  | ✅ Forces a new session                                      |
| `GET /v1/auth/status`    | ✅ Inspect current session                                   |
| `GET /v1/health`         | ✅ Upstream probe                                            |
| Auth providers           | `SessionToken` · `auth.json` · OAuth (stub)                 |
| Request passthrough       | Any extra field (`reasoning_effort`, `search`, …) reaches upstream |
| Conversation continuity | Stable `x-grok-agent-id` / `x-grok-session-id` / `x-grok-conv-id` |

## Quick start

### 1. Install

```bash
uv sync                                # or: pip install -e .
cp .env.example .env                   # then edit
```

### 2. Configure credentials

Three ways (pick exactly one):

```bash
# (a) The fast path: a SessionToken (see "Acquiring a SessionToken" below).
echo 'GROK_SESSION_TOKEN=eyJhbGciOi...' >> .env

# (b) Re-use the official CLI's own ~/.grok/auth.json.
echo 'GROK_AUTH_FILE=/Users/you/.grok/auth.json' >> .env

# (c) Headless OAuth -- interactive flow is a stub, see auth/providers.py.
```

### 3. Launch

```bash
uv run grokcli2api
# or
python -m grokcli2api --port 8088
```

Visit `http://127.0.0.1:8088/docs` for Swagger UI.

### 4. Call it like OpenAI

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8088/v1", api_key="not-used")
resp = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

### Stream:

```python
stream = client.chat.completions.create(
    model="grok-4",
    stream=True,
    messages=[{"role": "user", "content": "Tell me a joke"}],
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

## Network configuration

### Outbound proxy

By default GrokCli2API talks directly to `cli-chat-proxy.grok.com`. Operators
can route the connection through a forwarding proxy via `GROK_PROXY_URL`:

```bash
# mitmproxy local capture (HTTP)
GROK_PROXY_URL=http://127.0.0.1:8080

# SOCKS5 tunnel (requires `pip install httpx[socks]`)
GROK_PROXY_URL=socks5://user:pass@127.0.0.1:1080

# Corporate forward proxy
GROK_PROXY_URL=http://corp-proxy.internal:3128
```

Bypass patterns are configured via `GROK_NO_PROXY` and are mirrored onto the
`NO_PROXY` env var that `httpx` already understands:

```bash
GROK_NO_PROXY="localhost,127.0.0.1,*.internal,10.0.0.0/8"
```

If `GROK_PROXY_URL` is left unset, `httpx` falls back to the standard
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` env vars. `GROK_NO_PROXY`
patterns are still applied on top.

Toggling certificate verification for a corporate TLS-interception proxy:

```bash
GROK_TLS_INSECURE_SKIP_VERIFY=true   # only when you trust the MITM cert chain
```

## Acquiring a SessionToken

The fastest path is to grab one from the official Grok CLI:

1. Install `grok` from <https://x.ai/cli>.
2. Run `grok login` to complete OAuth.
3. Inspect your network egress (`mitmproxy` against `cli-chat-proxy.grok.com`)
   or use `strings ~/.grok/auth.json` to find the active bearer token.
4. Paste it into `GROK_SESSION_TOKEN`.

> Tokens are **bearer credentials** -- treat them like API keys. The
> official CLI refreshes them in the background; if you let yours expire,
> hit `POST /v1/auth/refresh` after wiring up an OAuth provider.

## How it mirrors the official chain

| Wire element                  | Source of truth in `grok.exe`                   |
| ----------------------------- | ----------------------------------------------- |
| `https://cli-chat-proxy.grok.com/v1` | `0x146afe3f9b` → `GROK_CLI_CHAT_PROXY_BASE_URL` |
| `x-grok-client-name`          | `env GROK_CLIENT_NAME` (default `xai-grok-cli`) |
| `x-grok-client-version`       | `env GROK_CLIENT_VERSION`                       |
| `x-grok-client-surface`       | `env GROK_CLIENT_SURFACE` (default `cli`)       |
| `x-grok-client-identifier`    | `env GROK_CLIENT_IDENTIFIER`                    |
| `x-grok-agent-id`             | Stable UUID generated at startup                |
| `x-grok-session-id`           | Stable UUID generated at startup                |
| `x-grok-conv-id`              | `user` field if provided, else UUID per request |
| `x-grok-req-id`               | Per-request `secrets.token_hex(16)`             |

The auth header `Authorization: Bearer …` is supplied by whichever
provider you wired up.

## Development

```bash
uv sync --extra dev
uv run pytest         # smoke + unit tests
uv run ruff check .
uv run grokcli2api --reload
```

## Project layout

```
grokcli2api/
├── __main__.py            # `python -m grokcli2api`
├── __init__.py
├── config.py              # pydantic-settings
├── server.py              # FastAPI app + routes
├── auth/
│   ├── session.py         # Session / SessionStore / AuthProvider ABC
│   └── providers.py       # SessionToken / AuthFile / OAuth
├── grok/
│   ├── client.py          # httpx-based reverse client
│   ├── headers.py         # x-grok-* header builder
│   └── models.py          # Static catalog
├── openai/
│   ├── schemas.py         # OpenAI-compatible Pydantic models
│   └── converter.py       # Format translation
└── utils/
    └── logger.py          # rich-based logging
```

## License & disclaimer

AGPL3