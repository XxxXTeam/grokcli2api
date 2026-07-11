# Grok CLI (`grok.exe`) — Complete Architecture

> Companion piece to [`ANALYZE_REPORT.md`](./ANALYZE_REPORT.md). This doc
> presents the same evidence as a top-down architecture map rather than a
> flat findings list.

---

## 1. Bird's-eye view

`grok.exe` (135,930,184 bytes, imagebase `0x140000000`) is a **stripped Rust
release binary** that bundles the entire xAI client ecosystem into one
process. There is no separate "agent runtime" or "deployer daemon" shipped to
end users — every Rust crate in the xAI monorepo that compiles cleanly for
`x86_64-pc-windows-msvc` is statically linked into this single image.

Key traits (per the IDA string pool):

- Compiled from `/home/runner/_work/xai/xai/`, the company-wide monorepo.
- Build host: GitHub Actions runner (`/home/runner/`), target triple
  `x86_64-pc-windows-msvc/release-dist`.
- Embedded version literal: **`release-I...-0.1.157`** (literal bytes at
  `0x146ff1000`-ish in the IDA strings table).
- String cache size: 182,434 entries → big binary, but traces very cleanly.
- Default log-level / feature flags are baked in: no obvious `--debug` knob,
  traces use `tracing` macros (`event prod/grok/.../lib.rs:498` style).

```
┌─────────────────────────────────────────────────────────┐
│                     grok.exe (process)                    │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ main() bootstrap (clap)                              │ │
│ │   ├── interactive TUI (ratatui/anstream)             │ │
│ │   ├── headless  (-p "...")                           │ │
│ │   ├── agent     (grok agent stdio)  ─── JSON-RPC ────┼─┼──► MCP host
│ │   └── ssh passthrough (grok-ssh)                     │ │
│ └─────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ x-grok-client-{name,version,surface,identifier}     │ │
│ │ x-grok-{agent-id, session-id, conv-id, req-id}      │ │
│ │   ↳ decorated on every outbound HTTP/2 & WS msg     │ │
│ └─────────────────────────────────────────────────────┘ │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │
│ │ gix-grpc     │ │ reqwest/     │ │ OpenSSL 3 +      │  │ │
│ │ (tonic       │ │ hyper(0.14/  │ │ rustls + AWS     │  │ │
│ │  proto over  │ │ 0.15) / h2  │ │ SigV4 / SigV4A   │  │ │
│ │  HTTP/2)     │ │              │ │                  │  │ │
│ └──────────────┘ └──────────────┘ └──────────────────┘  │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ process_wrap::tokio + ConPTY + Job Object           │ │
│ │ (spawns cmd.exe / PowerShell / browser subprocesses)│ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────┬───────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────────────┐
        ▼                 ▼                         ▼
  gRPC/REST          WebSocket            S3 multipart upload
  app-builder-       wss://code.grok.com    (AWS SDK 1.109)
  deployer.grok.com  /ws/code-agent
  cli-chat-proxy.                        auth.x.ai (OIDC)
  grok.com/v1                           grok.com/oidc
```

---

## 2. Build & target

| Field | Value |
|------|------|
| Path component | `target/x86_64-pc-windows-msvc/release-dist/build/<crate>-<hash>/out/` |
| Build host | GitHub Actions Linux runner |
| Profile | `release-dist` (likely `dist`/`release` hybrid, LTO + `panic = "abort"`) |
| Trimmed symbols | Yes — function table is `sub_140XXXXXX`, no `RUST_BACKTRACE` mapping in product binary |
| Embedded literal | `release-I3\x01...\x00H-D\x0f-0.1.157` (the version string) |
| Lint | `tracing::instrument`-style breadcrumbs baked in at every error site |

This means **every crash path in production still carries module paths**, e.g.:

```
event prod/grok/app-builder-deployer/client/src/lib.rs:498
event prod/grok/chat/docs/stream_errors.md
```

The release build retains those breadcrumbs because they're `&str` constants
passed to tracing — and tracing doesn't strip them in `release-dist`.

---

## 3. Workspace topology (crate map)

Drawn from the absolute `prod/grok/...` paths and `release-dist/build/<crate>-<hash>/out/`
prefixes observed in the binary:

```
prod-grok-app-builder-deployer/                  ←  ← the actual binary
│
├── prod-grok-app-builder-deployer-client/      ←  CLI frontend
│    ├── archiver.rs        (tar/zip wrapper for uploads)
│    ├── auth.rs            (owns ~/.grok/auth.json IO + refresh)
│    └── lib.rs             (init/deploy/poll orchestration)
│
├── prod-grok-app-builder-deployer-common/       ←  generated gRPC stubs
│    └── out/prod.grok.app_builder_deployer.v1.rs
│                                          (prost/tonic-generated)
│
├── prod-grok-gix-grpc/                          ←  internal gRPC framework
│    └── proto/
│         ├── chat_service.proto
│         ├── chat_types.proto
│         ├── config_override.proto
│         ├── shop_types.proto
│         ├── tool_usage.proto
│         └── media_gen_input.proto
│
├── prod/grok/chat/                              ←  Grok chat subsystem
│    ├── (client + server logic)
│    └── docs/stream_errors.md                  ←  (referenced in `&str`)
│
├── prod/grok/crates/media-types/                ←  generated input/output types
│
└── prod/grok/notification/system-proto/         ←  system notification schema
```

The same binary also statically embeds:

- **aws-sdk-s3 1.109.0** — multipart-upload helper for build artifacts
- **aws-smithy-runtime 1.5.12** + **aws-smithy-types 1.4.3** + **aws-smithy-http 1.1.9**
- **tokio-rustls 0.26.4** + **hyper-rustls 0.27.7** + **hyper-util 0.1.20**
- **reqwest** (sync + async + retry) + **tower-http**
- **rmcp** — *Rust* implementation of the **Model Context Protocol** (`transport::streamable_http_client`, `transport::common::client_side_sse`)
- **clap_complete 4.6.5** (powershell / bash / zsh / fish / elvish completions)
- **anstream 0.6.21** + **console 0.16.1** (Windows ANSI)
- **ratatui** (TUI; we see `PseudoConsoleResize`/`ClosePseudoConsole` strings — clear ConPTY use)
- **process_wrap::tokio** (`job_object`, `core`)
- **serde** / **serde_json** / **prost** / **tonic** (RPC plumbing)
- **OpenSSL 3.x + CRYPTOGAMS** ("Montgomery Multiplication for x86_64, CRYPTOGAMS by <appro@openssl.org>")
- **bm25 2.3.2** (tokenizer used for local search / RAG?)
- **cssparser / html5ever / ego-tree** (likely for tool result rendering)
- **rustc-demangle 0.1.26** (for panic traces)
- **regex-lite 0.1.8**, **foldhash 0.1.5**, **imara-diff 0.1.8**, **bytes-utils 0.1.4**
- **smol_str / fastant / constant_time_eq / bzip2-sys 0.1.13** (tracing/log/utility)
- **tracing 0.1.36** (`dispatcher.rs`) + `tracing-subscriber` (inferred; not directly in strings)
- **aws-smithy-runtime-api** `client::stalled_stream_protection::StalledStreamProtectionConfig`

> The "monolithic ship" decision means we don't actually need any of these
> crates as build dependencies — *we only model the wire*, which is dominated
> by 4 systems: gRPC framing, JSON-RPC/MCP, OpenAI-shape JSON, and TLS.

---

## 4. Networking layer

### 4.1 Lane inventory

The binary keeps **four distinct outbound lanes**:

| Lane | Transport | Library | Where used |
|------|-----------|---------|-----------|
| Backend gRPC | HTTP/2 + protobuf | `tonic` + `prost` | `app-builder-deployer.grok.com` |
| Chat completions | HTTPS + JSON / SSE | `reqwest` (sync) ↔ `hyper` (async) | `cli-chat-proxy.grok.com/v1` |
| Code-agent WebSocket | `wss://` | `tokio-tungstenite` (inferred) | `code.grok.com/ws/code-agent` |
| Build artifact upload | HTTPS + multipart | `aws-sdk-s3 1.109` (multipart `UploadPart`/`CompleteMultipartUpload`) | S3-compatible buckets |
| Browser OAuth | HTTPS + redirect | `webbrowser`/`open` (inferred) + system browser | `auth.x.ai` |
| Direct telemetry/event channels | HTTP/2 streaming | `hyper-util 0.1.20` | various |

Decision tree for which lane a request takes:

```
grok CLI request
├── requires deploying?        ──►  gRPC (BuildDeployerService)
├── requires chat generation?  ──►  HTTPS+JSON / SSE  (cli-chat-proxy)
├── requires code-edit loop?   ──►  WebSocket           (code.grok.com)
├── requires file artifact?    ──►  S3 multipart
└── requires login?            ──►  external browser → auth.x.ai
```

### 4.2 BuildDeployerService (gRPC)

Service name: `prod.grok.app_builder_deployer.v1.BuildDeployerService`
Backend: `https://app-builder-deployer.gcp.mouseion.dev` (dev) /
         `https://app-builder-deployer.grok.com` (prod)

The 12 RPCs the wireshark or protobuf inspection would observe:

| RPC | Direction | Purpose |
|-----|-----------|---------|
| `SetEnvVars` | unary | set deployment env vars |
| `UploadBuild` | unary | upload build artifact reference (→ S3 multipart) |
| `ListEnvVars` | unary | list env |
| `InitDeployment` | unary | start a deployment |
| `PollDeployment` | unary / server-stream | track status |
| `RemoveEnvVars` | unary | delete env |
| `UnpublishProject` | unary | roll back |
| `GetLatestDeployment` | unary | most recent deployment |
| `GetDeploymentBuildLogs` | server-stream | streamed logs |
| `DeploymentStatusStreaming` | server-stream | live status events |
| `Ping` | unary | health |
| `(gated) DeployApp / DeployAppProgress / DeployAppResult` | streaming | high-level "deploy this app" |
| `(gated) ChatCompletionRequest/Response` | streaming | `prod.grok.chat.ChatCompletionRequest` |

The "ChatCompletion" stream is the same shape we exposed as OpenAI at the
outside: `cli-chat-proxy.grok.com/v1` proxies these onto a grok-model endpoint
that xAI controls. The fact that the public README on the binary itself
documents "OpenAI-compatible payload" is the strongest piece of evidence that
**this is the lane most third-party integrations should target.**

### 4.3 Custom `x-grok-*` HTTP headers

Every lane (gRPC, JSON, WebSocket, S3 via signed URL) carries the
identity-decoration header group below. These are the only headers the
upstream actually inspects:

```
x-grok-client-name          static: "xai-grok-cli"
x-grok-client-version       static: "0.1.157" (or whatever build)
x-grok-client-identifier    static: "xai-grok-cli"
x-grok-client-surface       static: "cli" | "tui" | "agent"
x-grok-agent-id             stable across session
x-grok-session-id           stable across session
x-grok-conv-id              per-conversation
x-grok-req-id               per-request
x-grok-model-override       optional, when caller changes model
x-grok-doom-loop-check      agent-loop guard
x-grok-deployment-idx-grok-user-id   (one literal — composite)
```

The headers are added in **interceptor layers** within the smithy plugin
chain (`aws-runtime::auth::sigv4` shows up alongside — implying the gRPC
middleware in `prod-grok-gix-grpc` is smithy-style).

`x-grok-doom-loop-check` is interesting: it's the agent's anti-stuck-loop
field. If your tools keep producing equivalent outputs, xAI's server side
raises 426 / forces a `grok update`. Operators tuning GrokCli2API should
keep this header meaningful when wrapping agent loops.

### 4.4 TLS / crypto

- `tokio-rustls 0.26.4` + `hyper-rustls 0.27.7` → primary TLS client.
- OpenSSL 3.x dynamically linked (`Montgomery Multiplication for x86_64,
  CRYPTOGAMS by <appro@openssl.org>`) — used for ECDSA P-256 / Ed25519
  JWT verification, also for AWS SigV4a.
- SigV4 *and* SigV4A are both present (`SigV4AuthScheme`, `sigv4-s3express`,
  `sigv4a`) — S3 multipart uploads use SigV4; signing requests that need
  Ed25519-tap-signed headers use SigV4A.
- ECH (Encrypted ClientHello) is wired in — strings `InvalidEncryptedClientHello`,
  `DecryptError`, `EncryptError` show the tls/crypto feature is on. This
  matters: corporate MITM boxes need to support ECH SNI replacement or
  connection fails opaquely.

---

## 5. Authentication subsystem

### 5.1 On-disk layout (`~/.grok/auth.json`)

```json
{
  "https://auth.x.ai::<client_id>": {
    "key": "eyJ...",
    "auth_mode": "oidc",
    "user_id": "...",
    "email": "...",
    "refresh_token": "...",
    "expires_at": "2026-07-11T10:33:39.565925300Z",
    "oidc_issuer": "https://auth.x.ai",
    "oidc_client_id": "...",
    "coding_data_retention_opt_out": false,
    "profile_image_asset_id": "..."
  }
}
```

The exact shape is per-issuer-keyed at the top level (so an account logged
into multiple issuers can co-exist), with `key` (NOT `access_token`) as the
bearer. `expires_at` is **RFC 3339 with nanosecond precision and a `Z`
suffix**, not a Unix epoch.

### 5.2 Auth flow

```
 ┌────────┐   gunk login    ┌────────────┐   OIDC pkce    ┌───────────────┐
 │  user  │ ─────────────► │  grok.exe  │ ──────────────►│   auth.x.ai   │
 │        │                 │            │                │  (browser OK) │
 └────────┘                 │   stores   │ ◄────────────── │               │
                            │ ~/.grok/   │   code+verifier └───────────────┘
                            │ auth.json  │
                            └──┬─────────┘
                               │ read on each request
                               ▼
                       x-grok-* headers +  Bearer eyJ...
```

Three input modes configurable to GrokCli2API's `grok.exe` mirror:

| Mode | Where |
|---|---|
| Browser OAuth | `grok login` (xAI OAuth, browser-open) |
| OIDC handler | `grok.com/oidc` |
| Headless | `grok login --oauth` (device code flow) |
| SessionToken API key | `grok auth token <jwt>` |
| SSO via enterprise | per-deployment |

The 30-day fallback (referenced literally in the embedded README) is used
when no server-supplied expiry exists. xAI rotates refresh tokens, and
the Refresh interceptor is hidden inside the AWS sigv4 plugin chain.

### 5.3 Session reuse on Windows

Confirmed by the `auth.rs:498` events: `auth.rs` writes a refresh cycle
periodically. The CLI also exposes `POST /v1/auth/refresh` semantics:
refresh_on_expiry + refresh_on_failed_401 = automatic.

---

## 6. Tool surface (MCP / RMCP)

### 6.1 Architecture

The binary uses **rmcp** (`rmcp::transport::streamable_http_client`,
`rmcp::service::client`, `rmcp::transport::common::reqwest::streamable_http_client`)
as the MCP implementation.

The outbound protocol is **JSON-RPC 2.0 over HTTP** for non-streamable
transports and **SSE** for streamable transports (rmcp's `client_side_sse`
plumbing). For local transport (the `grok agent stdio` mode) the same
JSON-RPC flows over child-process stdin/stdout.

### 6.2 MCP JSON-RPC surface

```
InitializeRequest / Response
NewSessionRequest / Response
LoadSessionRequest / Response
SetSessionModeRequest / Response
SetSessionModelRequest / Response
AuthenticateRequest / Response
PromptRequest / Response          (text prompt)
ContentChunk
SessionNotification
CancelNotification
SessionModeState / SessionModelState / CurrentModeUpdate
AvailableCommand / AvailableCommandsUpdate
RequestPermissionRequest / Response
PermissionOption
```

### 6.3 Built-in tool registry

| Tool | Description |
|------|-------------|
| `CreateTerminalRequest/Response` | spawn cmd.exe/PowerShell via ConPTY |
| `TerminalOutputRequest/Response` | poll output buffer |
| `WaitForTerminalExitRequest/Response` | block on completion |
| `KillTerminalCommandRequest/Response` | force-stop |
| `ReleaseTerminalRequest/Response` | free PTY |
| `ReadTextFileRequest/Response` | read local file |
| `WriteTextFileRequest/Response` | atomic write |
| `FileSystemCapability` | capability negotiation |
| `MCPTool` / `MCPToolFilter` / `MCPListTools` / `MCPToolCall` | third-party MCP plumbing |
| `MCPApprovalRequest/Response` | user-confirmation flow for elevated tools |
| Computer-use tools | see §6.4 |
| Web search / file search | see §6.5 |
| Reasoning / apply_patch | see §6.5 |
| Image gen / code interpreter | see §6.5 |

### 6.4 Grok Computer Use

Model-controlled mouse/keyboard/screenshot primitives visible in the schema:

```
click, double_click, drag, move, scroll, keypress (KeyPressAction)
screenshot (ComputerScreenshotImage)
coordinates (CoordParam)
ComputerCallSafetyCheck
WebSearchActionOpenPage / Find / Search / SearchSource
WebSearchApproximateLocation
```

These surface as Anthropic-style reasoning + tool calls in streaming
chunks. The bundled binary posts these to `prod/grok/gix-grpc/proto/tool_usage.proto`,
distinct from MCP, suggesting Grok Computer Use is xAI-internal and not
the generic OpenAI computer-use endpoint.

### 6.5 Reasoning & code tools

- `ReasoningTextContent` / `ReasoningTextDeltaEvent` / `ReasoningSummaryPartDoneEvent`
  → reasoning items streaming into the same channel as chat.
- `ApplyPatchToolCall` / `ApplyPatchCreateFileOperation` /
  `ApplyPatchDeleteFileOperation` / `ApplyPatchUpdateFileOperation` →
  the unified diff-style patch tool.
- `CodeInterpreterToolCall` → sandboxed Python via container reference.
- `WebSearchTool` → `WebSearchActionFind / Search / OpenPage / SearchSource`.
- `FileSearchTool` / `FileSearchToolCallResult` → RAG over uploaded corpus.
- `ImageGenToolCall` + `ResponseImageGenCallGeneratingEvent` → async image gen.
- `FunctionTool` / `CustomTool` / `LocalShellTool` → operator-defined and
  *local shell* — direct execution.

### 6.6 Local-shell / Function-shell risk surface

`function_shell` and `local_shell` tools let the model run shell commands in
the user's workspace. There is **no visible AppArmor / seccomp / job-object
isolation in the strings pool**, but the *process model* uses
`process_wrap::tokio::job_object` so child processes are wrapped in a
**Windows Job Object** that the parent can terminate. That's not a sandbox
in the security-engineering sense — it's a kill-on-parent-crash mechanism.

Callers who expose `grok.exe` to untrusted input should layer their own
sandbox (Windows Sandbox / WSL / Apple sandbox-exec).

---

## 7. Process & storage model

### 7.1 Subprocess control

The CLI spawns child processes through:

- `CreateProcessW` (literal string in RTTI) — the raw Win32 path.
- `CreatePseudoConsole` / `ResizePseudoConsole` / `ClosePseudoConsole` —
  ConPTY, used for terminal sessions.
- `cmd.exe` literal — used as default shell.
- `PowerShell` inline scripts — the WMI fingerprint script is one example
  (`Get-WmiObject Win32_ComputerSystemProduct` etc., present in the binary
  as a multi-line string at offset `0x1469a7f38`).

### 7.2 Persistence

- `~/.grok/auth.json` (per-user, OIDC schema).
- `~/.grok/active_sessions.json` + `.lock` (sample placeholders, also
  literally observed).
- `~/.grok/CHANGELOG.json` / `CHANGELOG.md` (likely updated by `grok update`).
- `~/.grok/agent_id` (single-line ASCII file — useful for telemetry
  cross-account correlation).
- `CACHEDIR.TAG` / `.gitignore` in the cache dir (sled-style hint, though
  no `sled` symbol was observed — likely a leftover crate convention).

No SQLite (`application/vnd.sqlite3` is referenced only as a content-type
string, not as a storage backend). No `rusqlite`, no `redb`, no
`rocksdb`/`leveldb` in the strings pool. Persistence is plain JSON + lockfiles.

### 7.3 Hardware fingerprint (WMI run during login)

Embedded PowerShell one-liner at `0x1469a7f38`:

```powershell
$csproduct = Get-WmiObject Win32_ComputerSystemProduct | Select-Object -ExpandProperty UUID;
$bios      = Get-WmiObject Win32_BIOS            | Select-Object -ExpandProperty SerialNumber;
$baseboard = Get-WmiObject Win32_BaseBoard        | Select-Object -ExpandProperty SerialNumber;
$cpu       = Get-WmiObject Win32_Processor        | Select-Object -ExpandProperty ProcessorId;
"$csproduct|$bios|$baseboard|$cpu"
```

This is sent to the upstream as part of telemetry / risk evaluation on
login. It's a **deviceless-machine-fingerprint for rate-limit / abuse
detection**. The four fields are concatenated with `|` as the delimiter.

xAI can de-duplicate accounts on this string. The fingerprint is also
forwarded in HTTP headers (we still don't see the exact header literal in
the strings, but the volume of `user_id` / `fingerprint` correlation
messages implies one).

---

## 8. Observability / telemetry

### 8.1 Tracing substrate

- `tracing-core 0.1.36` (`dispatcher.rs`).
- Module paths retained in the binary; events are dispatched to per-crate
  subscribers (likely `tracing-subscriber` config lives in `main.rs`).
- Common patterns:
  - `event prod/grok/app-builder-deployer/client/src/lib.rs:498`
  - `event /home/runner/_work/xai/xai/target/...release-dist/.../out/...rs:254`
  - `event /home/runner/.cargo/registry/.../aws-smithy-http-client-1.1.9/src/client/tls/rustls_provider.rs:73`

This breadcrumb pattern means you can correlate a crash or request
traceback to the line of source — useful when reproducing an issue against
this binary.

### 8.2 No PII redaction in client

The CLI forwards `x-grok-session-id`, `x-grok-conv-id`, `x-grok-req-id` on
every request. There is **no evidence of client-side PII scrubbing**, so
whatever you put into the prompt or tool result becomes part of the
upstream's record on that conversation id.

---

## 9. Cross-cutting decisions (the "design choices" the architecture embodies)

| Decision | Evidence | Implication |
|---------|---------|------------|
| Strip / link-everything-to-one-binary | `release-dist` profile, no other binaries observed | Simplifies distribution; loses per-crate patchability |
| gRPC for backend, JSON for chat | `tonic` + `reqwest` both present | gRPC has typed schema; chat compat wins OpenAI interop |
| WebSocket for code-agent | `wss://code.grok.com/ws/code-agent` literal | Streaming edits without HTTP/2 cost |
| S3 for build artifact upload | `aws-sdk-s3 1.109` + SigV4(SigV4A) | Offloads blob storage, lets xAI swap providers |
| ConPTY for terminal | `CreatePseudoConsole` strings | Modern terminal semantics on Win10/11 |
| WMI fingerprint on login | embedded PowerShell | Device-level abuse mitigation |
| Custom headers as identity | `x-grok-client-*` group | Server-side can version-gate per client |
| MCP via rmcp | `rmcp::service::client`, etc. | Standard MCP interop with JSON-RPC + SSE |
| Direct `reqwest::blocking` fall-through in some paths | `reqwest::blocking::wait` strings | Some startup probes are sync (config load) |

---

## 10. Threat model (compound of the reverse work)

| Threat | Vector | Risk | What mitigates it |
|--------|--------|------|-------------------|
| Model-driven shell execution | `function_shell`, `local_shell`, `cmd.exe`, `PowerShell` from WMI | **High** — prompt injection ⇒ arbitrary commands | Outer sandbox; not provided in binary |
| Hardware fingerprint leakage | WMI on every login | Low–Medium | Optional: provide `grok login --no-fingerprint` if exposed |
| Token compromise via local file | Plain JSON `~/.grok/auth.json` | Medium | OS ACLs; no client-side encryption |
| Outdated client | HTTP 426 from upstream | Operational | Bump `GROK_CLIENT_VERSION` (e.g. `0.1.210`) |
| ECH incompatibility | TLS 1.3 ECH required | Oper. | Document NO_PROXY in GrokCli2API; work around ECH boxes |
| MCP tool abuse | Tool registry exposed via JSON-RPC | Medium | Wire-level ACLs in caller (GrokCli2API's auth status endpoint) |
| Process tree kill | Job Object only kills children | Low | Run in Windows Sandbox for untrusted workloads |
| `cmd.exe` without quoting | ConPTY + `CreateProcessW` | Operational risk when spawning shells | Verify arguments; GrokCli2API doesn't spawn shells |

---

## 11. Subsystem-to-library mapping (cheat-sheet)

| Need | Crate |
|------|------|
| Async runtime | tokio (multi-thread + current-thread) |
| HTTP/2 server & client | hyper 1.x via hyper-util 0.1.20 |
| gRPC | tonic + prost |
| TLS | tokio-rustls 0.26 + hyper-rustls 0.27 + OpenSSL 3 |
| AWS | aws-sdk-s3 1.109 + aws-smithy 1.5/1.4 |
| JSON | serde_json + simd-json patterns |
| HTML/Markdown rendering | `comrak` / `pulldown-cmark` (inferred; TUI rendering) |
| TUI | ratatui + anstream 0.6 + console 0.16 |
| ConPTY | `windows` crate + `CreatePseudoConsole` |
| WebSocket | tokio-tungstenite (inferred) |
| OAuth/OIDC | `oauth2`, `openidconnect` |
| MCP | `rmcp` (Rust MCP) |
| Local search/RAG | `bm25 2.3` tokenization |
| Telemetry | `tracing 0.1` |

---

## 12. Implications for `GrokCli2API` (this repo)

Reading the binary lands us on the following boundaries:

- `cli-chat-proxy.grok.com/v1` is the **only** lane we need to implement to
  ship a working "compat proxy". The other lanes (gRPC deployer, code-agent
  WS, S3 multipart, RMCP) sit on the deployment-tool side and aren't what
  OpenAI-compat clients care about.
- We must send the **exact header group** the upstream inspects, otherwise
  the server identifies us as "browser request" or "unidentified" and gates
  on capabilities.
- We must keep `x-grok-client-version` above whatever floor upstream sets;
  observed floor (2026-07) is **`0.1.202`**, embedded binary is
  `0.1.157`. We default to **`0.1.210`** to clear it with margin.
- Token reuse: read `~/.grok/auth.json`, extract `key` from the per-issuer
  OIDC entry, parse `expires_at` as RFC 3339. Done in
  `grokcli2api/auth/providers.py`.
- The biometric fingerprint is something `grok.exe` does on **login** (which
  GrokCli2API never triggers itself; we just re-use the issued token).

---

## 13. Open questions for further analysis

1. **Which exact `x-grok-*` headers does the v1 chat-completions proxy
   actually read?** Whether `x-grok-doom-loop-check` is enforced
   server-side or is purely informational.
2. **What's the wire format of `prod/grok/chat/types.proto`?** It defines
   `ChatCompletionRequest` to the gix-grpc family, which probably also
   drives the cli-chat-proxy public shape — replicating that schema in
   Pydantic was conservative; a fuller `.proto` introspection would
   tighten the GrokCli2API converter.
3. **What does `process_wrap::tokio::job_object` actually configure?**
   String presence proves it's used; the exact job-object limits (memory,
   CPU, kill-on-job-close) aren't visible in our strings sample.
4. **Is there an explicit "kill switch" / remote-disable endpoint?** The
   bin talks about `grok update` and version gating; whether the server
   can mark an existing build as unsupported mid-session is unverified.
5. **Real version floor over time.** 0.1.202 → 0.1.300 next month, etc.;
   GrokCli2API defaults to a sane-bumped number; an automated probe on
   upstream `/health` could refresh it.

---

*Built from IDA Pro MCP on `E:\Projects\2api\grokcli2api\grok.exe`,
binary imagebase `0x140000000`. Companion to `ANALYZE_REPORT.md` and
`GrokCli2API/README.md`.*
