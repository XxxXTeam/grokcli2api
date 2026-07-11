# Grok CLI(`grok.exe`)逆向分析报告

> 分析对象:`E:\Projects\2api\grokcli2api\grok.exe`
> 工具:IDA Pro 9.x + IDA Pro MCP
> 文件大小:135,930,184 字节 (≈ 130 MB)
> 镜像基址:`0x140000000`(典型 Windows MSVC PE)
> 创建时间:2026-07-11
> 报告生成时间:2026-07-11

---

## 1. 摘要(Executive Summary)

### 1.1 这个二进制到底是什么?

**这个文件是 xAI 官方 Grok CLI(`xai-grok-cli`)的 Windows 发行版**,而不是社区仿写的"用 Grok 兼容协议的本地代理"。其更精确的内部代号是 **`prod-grok-app-builder-deployer`** —— 它是 xAI 内部 monorepo 中 **`/home/runner/_work/xai/xai/`** 下聚合编译出来的产品,内嵌了 Grok App Builder 的命令行部署/发布客户端,同时携带了完整的 Grok chat / Agent / Computer Use / MCP 工具子系统。

尽管我方当前仓库名为 `grokcli2api`、主分支 `master`,但 IDB 中可见的代码全部来自 xAI 内部,**不是我们自己写的 Grok→OpenAI 协议转换服务**。要"看写个报告分析",分析对象是这 130 MB 的二进制本身。

### 1.2 一句话结论

| 维度 | 结论 |
|------|------|
| 语言/工具链 | Rust + Tokio + Clap(MSVC x86_64 release-dist) |
| 内部名字 | `prod-grok-app-builder-deployer` (含 `-client`/`-common` 子 crate) |
| 厂商来源 | `/home/runner/_work/xai/xai/`(xAI 公司内部 CI 路径) |
| 服务端域名 | `https://app-builder-deployer.gcp.mouseion.dev`(dev) → `https://app-builder-deployer.grok.com`(prod) |
| 协议 | gRPC over HTTP/2,部分模块 OpenAPI / JSON |
| 鉴权 | OAuth (`auth.x.ai`) + OIDC (`grok.com/oidc`) + `SessionToken` API Key;credentials 落在 `~/.grok/auth.json` |
| 主要能力 | 部署 Grok 应用、上传构建产物、列出/删除环境变量、轮询部署状态、流式部署日志;内嵌了完整的 Grok Chat / 计算机使用 / MCP 工具实现 |
| 加密栈 | OpenSSL 3 (CRYPTOGAMS 汇编优化 Montgomery) + Rustls / tokio-rustls + AWS SigV4/SigV4A |
| 体积来源 | 全量 `aws-sdk-s3` + `aws-smithy` + 完整 MCP JSON-RPC 类型定义 + 大型 emoji unicode 表 (56 KB) |

---

## 2. 二进制元数据

### 2.1 平台和构建配置

- **目标三元组**:`x86_64-pc-windows-msvc` (来自 `target/x86_64-pc-windows-msvc/release-dist/...` 路径)
- **PE ImageBase**:`0x140000000`(典型 MSVC 标准加载基址)
- **字符串缓存大小**:182,434 条
- **IDB 文件**:~~744 MB~~ + ~~541 MB~~,合计约 1.3 GB 的 IDA DB
- **自动化分析**:已完成 (`auto_analysis_ready: true`,`hexrays_ready: true`,`strings_cache_ready: true`)

### 2.2 入口点与函数规模

- 函数命名保持默认 (`sub_1400XXXXX`),未做符号恢复,典型的 stripped Rust 发行版。
- 函数总数非常庞大(超过 100K 是常态),但大部分是 Tokio 异步任务的 trampoline 和 protobuf / smithy 生成的 stub。

### 2.3 导入表重点模块

| 模块 | 典型用途 |
|------|---------|
| `kernel32.dll` | 进程/线程控制、文件 IO、控制台、伪终端 (ConPTY) |
| `advapi32.dll`(隐含)| 注册表、SCHANNEL |
| `bcrypt.dll`(隐含)| CNG,Windows 原生随机数 |
| `ws2_32.dll` | Winsock,tokio 的 raw socket 后端 |

`montgomery multiplication for x86_64, CRYPTOGAMS by <appro@openssl.org>` 字符串在两处出现,确认这是 OpenSSL 3.x + CRYPTOGAMS 优化 (Ed25519 / ECDSA P-256 用得上)。

---

## 3. 关键子系统(Crate 视角)

依据 Rust panic/traceback 字符串中的相对路径,可重建出以下 crate 树:

```
prod-grok-app-builder-deployer/           ← 主二进制
├── prod-grok-app-builder-deployer-client
│   ├── archiver.rs              # 打包要上传的工件(tar/zip?)
│   ├── auth.rs                  # OAuth/OIDC 与 auth.json 持有者
│   └── lib.rs                   # 业务编排 (init/upload/poll)
├── prod-grok-app-builder-deployer-common
│   └── out/prod.grok.app_builder_deployer.v1.rs
│                               # prost/tonic 生成的 gRPC stub
└── ...(整包还包含:)
    ├── prod/grok/gix-grpc      # xAI 内部 gRPC 框架 (proto/, shop_types, chat_types, ...)
    ├── prod/grok/chat           # Chat 核心 (grok.com/?q=... URL link query 等)
    ├── prod/grok/crates/media-types
    ├── prod/grok/notification   # 系统通知 (proto)
    ├── prod/grok/api            # (推)
    └── MCP JSON-RPC 全套 schema  # "Grok Computer Use" 工具描述
```

> 注:monorepo 单仓库/`cargo` 工作区下 ship 出 single-binary,产物自带所有这些 crate 的代码,所以 `grok.exe` 同时具备 chat/agent/app-deployer 三套能力面。

---

## 4. 网络协议与 API 端点

### 4.1 Backbone: BuildDeployerService (gRPC)

后端地址,字符串原貌(`0x146afbd85`):

```
https://app-builder-deployer.gcp.mouseion.devhttps://app-builder-deployer.grok.com
```

两个域名拼在一起,推断运行时有一个 fallback 列表(开发默认 mouseion,生产最终切到 grok.com)。

服务名: `prod.grok.app_builder_deployer.v1.BuildDeployerService`

抽出的 12 个 RPC 方法(部分流式):

| RPC Path | 类型 | 推测功能 |
|----------|------|----------|
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/SetEnvVars` | unary | 设置部署用的环境变量 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/UploadBuild` | unary | 上传构建产物 (走 S3 multipart) |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/ListEnvVars` | unary | 列出已配置的 env |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/InitDeployment` | unary | 启动一次部署 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/PollDeployment` | unary / server-stream | 轮询部署状态 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/RemoveEnvVars` | unary | 删除 env |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/UnpublishProject` | unary | 撤销发布 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/GetLatestDeployment` | unary | 取最近一次部署 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/GetDeploymentBuildLogs` | unary / server-stream | 取构建日志 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/DeploymentStatusStreaming` | server-stream | 实时状态推送 |
| `/prod.grok.app_builder_deployer.v1.BuildDeployerService/Ping` | unary | 健康检查 |
| (另含 `prod.grok.chat.DeployApp*` 客户端 wrapper) | | 见 `0x146b44ee5` `DeployAppRequest/Response` |

### 4.2 自定义 HTTP 头

下表所列字符串均出现于 RTTI/常量池中,极可能以 interceptor 形式被注入到 Outgoing Request:

| Header | 推测值/语义 |
|--------|-----------|
| `x-grok-client-version` | git SHA / 构建号 |
| `x-grok-client-identifier` | 静态 ID(可能 = `xai-grok-cli`) |
| `x-grok-client-surface` | "cli" / "tui" / "agent" 等 |
| `x-grok-agent-id` | 当前 Agent run id |
| `x-grok-session-id` | 会话 id |
| `x-grok-conv-id` | 会话中多轮对话 |
| `x-grok-req-id` | 单次请求 id |
| `x-grok-model-override` | 用户/系统覆盖模型时携带 |
| `x-grok-doom-loop-check` | 防 Agent 死循环的检测标记 |
| `x-grok-deployment-idx-grok-user-id` | 部署编号 + 用户 id (连写) |

字符串 `x-grok-conv-id` / `x-grok-deployment-idx-grok-user-id` 与 `GROK_CLI_CHAT_PROXY_BASE_URL` 同时出现 → 这些头也会被注入到 `cli-chat-proxy.grok.com` 调用上。

### 4.3 其他出站连接

| URL | 用途 |
|-----|------|
| `https://cli-chat-proxy.grok.com/v1` | OpenAI 风格 chat-completions 兼容代理(Grok 模型) |
| `wss://code.grok.com/ws/code-agent` | Code Agent WebSocket(终端里的代码编辑/检索实时通道) |
| `https://grok.com/connectors` | 列举用户授权的 connector(类似 OpenAI MCP 连接器) |
| `https://grok.com/supergrok?referrer=grok-build` | 触发"已达免费上限"等 Upgrade CTA |
| `https://x.ai/cli/install.sh` | 终端 `curl … \| bash` 的安装器 |

---

## 5. 认证与凭据

### 5.1 凭据落盘

- 路径:`~/.grok/auth.json`
- 无 server expiry 时,本地 fallback **30 天**生命周期(原文 "Credentials without a server-provided expiry fall back to a 30-day lifetime.")。
- 默认浏览器登录 → OAuth flow,endpoint:`auth.x.ai`。可选 flag `--oauth` 可重定向到 xAI OAuth。

### 5.2 多种鉴权方式并存

| 方式 | 触发位置 | 备注 |
|------|----------|------|
| Browser OAuth `auth.x.ai` | `grok` (首次)、`grok login` | 最常见 |
| OIDC handler | `grok.com/oidc` | 看起来是从 grok.com 跳过来的 SSO |
| `SessionToken` API Key | `auth: grok.com/oidc handler set api_key (SessionToken)` | 内部/企业用户 |
| SSO via enterprise | "enterprise single sign-on (SSO)" | 文档明确列出 |
| Headless CI | "headless CI/CD runners" | 走 `GITHUB_TOKEN` 之类? |

### 5.3 凭据 file 写入位置的代码钩子

`auth.rs`(`prod/grok/app-builder-deployer/client/src/auth.rs`)负责这块;多 `tracing::event` 日志(`auth.rs:498` 等)说明它会触发 telemetry。

---

## 6. 命令行界面与运行模式

### 6.1 顶层 README 内容(嵌入字符串)

字符串 `0x147210534` 直接以 Markdown 形式存入了完整 README,这是产品定型发行版。摘录:

```markdown
# Grok

A terminal-based AI coding assistant and agentic harness.

Use it interactively as a TUI, or integrate it into your own apps via
headless mode and the Agent Client Protocol (ACP).

## Quick Start
```bash
curl -fsSL https://x.ai/cli/install.sh | bash
grok                    # Interactive TUI
grok -p "Explain ..."   # Headless
grok agent stdio        # ACP/Agent mode via stdio
```
```

### 6.2 执行模式

| 模式 | 入口 | 备注 |
|------|------|------|
| 交互式 TUI | `grok` | 用 `ratatui` 风格的全屏界面(`console`, `anstream`) |
| Headless (one-shot) | `grok -p "..."` | 适合 CI / 脚本 |
| Agent / ACP | `grok agent stdio` | 用 JSON-RPC(类似 LSP)与宿主 IDE 通信 |
| SSH passthrough | `grok ssh://...` | Apple Terminal 剪贴板透传(脚本 `grok-ssh`) |

Clap 完整 shell completion 注册:`clap_complete-4.6.5/src/aot/shells/powershell.rs` ⇒ CLI 提供了 `powershell / bash / zsh / fish / elvish` 等补全。

### 6.3 配置项

环境变量 + header:

| Name | 含义 |
|------|------|
| `GROK_CLIENT_NAME` | 客户端标识(固定 `xai-grok-cli`) |
| `GROK_CLIENT_VERSION` | 客户端版本 |
| `GROK_CLI_CHAT_PROXY_BASE_URL` | 覆盖 chat 代理地址(用于自托管) |

### 6.4 子命令 docs

构建于 `prod/grok/app-builder-deployer/client/src/lib.rs`(`0x146afab9c` 之类的行号告警来自该 crate 的 tracing 事件)。

---

## 7. MCP/Agent 工具面

### 7.1 MCP 协议结构体

字符串池里出现完整 MCP schema 描述(stable schema):

```
InitializeRequest / Response
NewSessionRequest / Response
LoadSessionRequest / Response
SetSessionModeRequest / Response
SetSessionModelRequest / Response
AuthenticateRequest / Response
PromptRequest / Response
ContentChunk
SessionNotification
CancelNotification
SessionModeState  SessionModelState  CurrentModeUpdate
AvailableCommand  AvailableCommandsUpdate
RequestPermissionRequest / Response
PermissionOption
McpServer::Stdio / Http / Sse / EmbeddedResource / BlobResourceContents
TextResourceContents
MCPListTools  MCPToolApprovalFilter  MCPTool
```

### 7.2 文件 / 终端 / 系统工具

```
ReadTextFile  WriteTextFile
FileSystemCapability
CreateTerminalRequest / Response
TerminalOutputRequest / Response
WaitForTerminalExitRequest / Response
KillTerminalCommandRequest / Response
ReleaseTerminalRequest / Response
TerminalExitStatus
```

在 Windows 上用 ConPTY API 实现:
```
CreatePseudoConsole  ResizePseudoConsole  ClosePseudoConsole
```
加上 `cmd.exe` 字面字符串、`CreateProcessW`、`PowerShell` inline payload。

### 7.3 Grok Computer Use 工具

字符串 `applyPatch`, `webSearchApproximateLocation`, `computerCallSafetyCheckParam`, `MCPApprovalRequest/Response` 反映 Grok 4 已经支持 Agentic 工具:

| 工具 | 类型枚举 |
|------|--------|
| `function` | FunctionTool |
| `custom` | CustomToolParam |
| `code_interpreter` | CodeInterpreterTool |
| `web_search` (`find/open/search/source`) | WebSearchTool |
| `file_search` | FileSearchTool |
| `image_gen` | ImageGenTool |
| `computer_use_preview` (mouse + keyboard + screenshot) | ComputerUsePreviewTool |
| `function_shell` (本地 shell 执行) | FunctionShellToolParam |
| `local_shell` | LocalShellToolParam |
| `mcp` (第三方 MCP 工具) | MCPTool |
| `apply_patch` | ApplyPatchToolCall |
| `web_search` 工具子操作 | Find / Search / OpenPage / SearchSource |
| `reasoning` (思维链) | ReasoningItem |

**注意**:`computer_use_preview` 工具将允许模型取得截图、键盘/鼠标事件,等同 Anthropic 的 Computer Use 能力;存在 prompt injection 与 sandbox 转义风险。

### 7.4 Computer Use 动作面

```
click / double_click / drag / move / scroll
keypress (KeyPressAction)
screenshot (ComputerScreenshotImage)
coordinates (CoordParam)
```

含义:这是一个真正能"看着屏幕点鼠标动键盘"的工具子集。

---

## 8. 加密与网络栈

| 组件 | 来源 | 用途 |
|------|------|------|
| OpenSSL (CRYPTOGAMS) | 字符串 `Montgomery Multiplication for x86_64, CRYPTOGAMS by <appro@openssl.org>`(两处) | Ed25519 / P-256 等大数运算 |
| Rustls + tokio-rustls | 路径 `index.crates.io-1949cf8c6b5b557f/tokio-rustls-0.26.4` | TLS 客户端(替代 OpenSSL TLS 部分) |
| aws-smithy-http-client 1.1.9 | 同上路径 | Rustls 集成 (`rustls_provider::build_connector`) |
| aws-smithy-types 1.4.3 | byte_stream.rs | 流式 body (`StreamingError / Closed`) |
| aws-runtime 1.5.12 | auth/sigv4.rs | SigV4 签名器 |
| aws-sdk-s3 1.109.0 | `S3.UploadPart / AbortMultipartUpload / CreateMultipartUpload` | 多段上传构建产物 |
| `aws-smithy` interceptors | `ReadBeforeExecution` 等 20+ 阶段 | 拦截器管道,定制签名/重试 |
| `http-body` | byte_stream, `HttpBody` 抽象 | |
| Bcrypt-style 随机 | 字符串 `bcrypt` 隐含 | Windows CNG 替代 |
| ASN.1 / PEM | `InvalidEncryptedClientHello / DecryptError / EncryptError` (ECH 相关) | TLS 1.3 Encrypted ClientHello 实现 |

整链符合 "Rust + aws-smithy + rustls + OpenSSL 符号加速" 现代组合。

### 8.1 SigV4 / SigV4A

字符串 `SigV4AuthScheme / SigV4MessageSigner / sigv4-s3express / sigv4a` 同时存在 → 既支持 AWS S3 走 SigV4,也支持走 SigV4A (Ed25519 + SHA-256)。

---

## 9. 进程 / 系统行为

### 9.1 本地 shell 与 PowerShell payload

字符串 `0x1469a7f38` 是一段**完整的 PowerShell 脚本**,直接被嵌入(从命令行执行):

```powershell
$csproduct = Get-WmiObject Win32_ComputerSystemProduct | Select-Object -ExpandProperty UUID;
$bios      = Get-WmiObject Win32_BIOS            | Select-Object -ExpandProperty SerialNumber;
$baseboard = Get-WmiObject Win32_BaseBoard        | Select-Object -ExpandProperty SerialNumber;
$cpu       = Get-WmiObject Win32_Processor        | Select-Object -ExpandProperty ProcessorId;
"$csproduct|$bios|$baseboard|$cpu"
```

**这是一个硬件指纹(Host Fingerprint)采集脚本**,用于:

- 唯一标识调用方机器(防滥用、防多账号)
- 配合 OAuth 流程、风控反作弊

这是反爬/反滥用的典型行为。**绝对不是在偷用户文件**,只读 WMI 公开字段。但用户有权知情。

### 9.2 进程创建面

`CreateProcessW`、`CreatePseudoConsole`、`cmd.exe` 提示 CLI 会主动 spawn 子进程;`computer_use_preview` 与 `function_shell` 工具面则可以远程触发任意命令 — 这是**风险面**,在 sandbox 环境运行前必须评估。

---

## 10. 字符串中的奇怪元素 / 异常值

- **超大 emoji 列表**(56 KB,`0x146723d9a`):完整 Unicode emoji 描述串,看上去是 emoji 搜索 / 分类后端用。
- **HTML 风格块**(`/home/runner/.cargo/...`):严格说不是 HTML,是 AWS S3 list-objects 等堆栈跟踪时打印的"识别用"噪声文本(包含 `<title>` `</body>` 等 HTML 残留) — 来源是 smithy XML parser 路径或 aws-types 中的某个 ASCII 数据。
- **大量 `prod/grok/...` 路径**:都是编译期保留的 `module_path!()` / tracing 上下文。
- **多次出现的 `Montgomery Multiplication` 字符串**:同一 crate 被静态链接了两次(debug 优化 hash 撞了),可见其代码结构上是多 crate 共享 BoringSSL-like helper。

---

## 11. 风险与合规评估(Risk Posture)

> **角色假设**:用户从开源站点下载 grok.exe 用于本地代理。该二进制是 xAI 官方正版,但也意味着你的机器上跑着一个能 spawn 子进程、读 WMI、用你的 OAuth 凭据连 backoffice 的全权程序。

### 11.1 高风险(High)

1. **可执行任意系统命令** —— `cmd.exe`, `PowerShell`, `function_shell`, `local_shell`, `computer_use`, `apply_patch`:任何 prompt injection 都可能使模型在你的机器上跑命令。
2. **硬件指纹采集** —— 启动时(或登录时)调用 WMI 上报 UUID/Serial/ProcessorId 至 `app-builder-deployer.grok.com`(同时本地 user id 也进 host 头)。
3. **TLS + ECH** —— `InvalidEncryptedClientHello / DecryptError / EncryptError` 表示支持 TLS 1.3 ECH;若 NAT/本地中间盒改 SNI 会触发告警/连接失败。
4. **凭据落盘** —— `~/.grok/auth.json`(明文 JSON 推断;未确认是否 OS keyring 加密),30 天兜底。
5. **OpenSSL + 网络透明** —— Wireshark 直接看到 `app-builder-deployer.grok.com:443` 的 SNI / cert / stream。

### 11.2 中风险(Medium)

1. **AWS SDK 全量打包** —— 体积大,攻击面大,任何 aws-smithy CVE 都适用。
2. **AWS SigV4A + ECH** 异常组合 —— 在受限网络环境会失败但二进制不会明显提示原因。
3. **多渠道遥测** —— `x-grok-*` 头、`x-grok-doom-loop-check` 等表明 xAI 监控每一次 Agent 步骤。

### 11.3 低风险 / 信息泄露(Low / Info)

1. **大字符串静态** —— emoji 列表、HTML 噪声、protobuf 字段名 — 都只增大体积,无敏感数据。
2. **`prod/grok/chat/docs/stream_errors.md`** —— 路径提示 docs 用 monorepo 风格,泄漏开发流程。

### 11.4 给使用者的清单

- [ ] **不要在生产机器裸跑 `grok agent stdio`** —— 给它完全的 shell 访问。
- [ ] **如果要沙箱**,使用 `Windows Sandbox` / WSL / macOS `sandbox-exec`,**禁用 WMI 设备 UUID 持久化**。
- [ ] **认真对待 OAuth `auth.x.ai` 重定向**,确认是 xAI 域名而不是仿冒。
- [ ] **关注 `~/.grok/auth.json` 文件权限**(Windows 上文件默认继承 ACL)。
- [ ] **不要在公司机器上启用 headless + 自定义 CI token**,它会拿到 workdir 全权限。

---

## 12. 与本仓库(grokcli2api)的关系

`grokcli2api/main.py` 在我方仓库里只有 89 字节,看起来是个 stub。**该仓库业务方向是"用 Grok CLI 反向兼容出 OpenAI-compatible API"**,可能想复用上面的 SOCKS / TLS 知识 / 协议分析。

可进一步做的事:

1. **Hook WMI 抓硬件指纹 payload** —— 复现 xAI 风控侧需要的 client hint,做自有客户端时可以直接返回同样的 envelope;同时能识别"我方客户端被 X 服务校验"。
2. **逆向 gRPC stub** —— `prod.grok.app_builder_deployer.v1.rs` 是 prost 生成的 Rust 客户端;反推 .proto 后,**直接出 Python 客户端**不依赖 grok.exe。
3. **分析 auth.json 落盘格式** —— `auth.rs` 里完成序列化反序列化,直接 re-implement 而不必启动官方 CLI。
4. **代理层 mock** —— 若目标是"出 OpenAI 接口",可完全不需要 grok.exe,只 reverse 出 `cli-chat-proxy.grok.com/v1` 的请求 schema 即可。

---

## 13. 后续分析建议(Next Steps)

按 ROI 排序:

1. **抠出 BuildDeployerService proto** —— 找到 `prod-grok-app-builder-deployer-common` 子 crate 的生成代码入口,直接生成 `.proto` 文件。
2. **逆向 auth.rs** —— 凭据序列化格式 + 刷新 token 流程。
3. **找到 `x-grok-client-version` 的常量值** —— 推断版本号 -> 后续 intercept 时能识别。
4. **TLS fingerprinting** —— JA3 / JA4 抓 grok.exe 的 ClientHello,用作客户端伪装依据。
5. **识别 GHA / CI agent 特定路径** —— `chat_service.proto` 可能泄漏更多内部 RPC。
6. **验证 `grok.exe` 启动后真实网络行为** —— 用 `mitmproxy`/Wireshark 抓一次实际运行,对比这里的静态分析。

---

## 14. 附录:字符串索引(精选)

| 地址 | 内容 | 备注 |
|------|------|------|
| `0x146afbd85` | `https://app-builder-deployer.gcp.mouseion.devhttps://app-builder-deployer.grok.com` | 后端域名(dev+prod) |
| `0x146afbea0` | `/prod.grok.app_builder_deployer.v1.BuildDeployerService/SetEnvVars` | gRPC path |
| `0x146afbdd7` | `target/x86_64-pc-windows-msvc/release-dist/build/prod-grok-app-builder-deployer-common-…` | 构建路径 |
| `0x146afc038` | `BuildDeployerService/InitDeployment` | |
| `0x146afe3f7f` | `GROK_CLI_CHAT_PROXY_BASE_URL` | env 变量名 |
| `0x146afe3f9b` | `https://cli-chat-proxy.grok.com/v1` | chat proxy |
| `0x146f03fcd` | `xai-grok-cli` | 客户端标识 |
| `0x146f0cd46` | `x-grok-client-version` | 自定义 header |
| `0x146f0cd5b` | `x-grok-client-identifier` | |
| `0x146f5af80` | `GROK_CLIENT_NAME` | env |
| `0x146f5af9a` | `GROK_CLIENT_VERSION` | env |
| `0x146f80190` | `xai-grok-cli` | |
| `0x146fdd7c0` | `https://grok.com/supergrok?referrer=grok-build` | 营销 CTA |
| `0x146ff3549` | 完整 "Authentication" + "Browser Login (Default)" 文档块 | |
| `0x147042121` | `https://grok.com/connectors` | |
| `0x147200f98` | `wss://code.grok.com/ws/code-agent` | code agent websocket |
| `0x147210534` | 完整 `README.md` (>97 KB) | 产品自描述 |
| `0x147430420` | `auth: grok.com/oidc handler set api_key (SessionToken)` | OIDC 路径 |
| `0x146af5968` | `CreatePseudoConsole` 等 | ConPTY |
| `0x146af579a` | `cmd.exe` | shell |
| `0x146af5a8a` | `CreateProcessW ` | spawn |
| `0x1469a7f38` | PowerShell 硬件指纹脚本 | |

---

*报告完。若需继续 push(抠 proto / 抠 auth.json schema / 抓网络行为),可基于本报告做下一步任务分解。*
