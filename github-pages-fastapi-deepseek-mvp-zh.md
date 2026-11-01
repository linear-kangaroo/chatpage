# MVP 实施方案：GitHub Pages + Windows FastAPI + DeepSeek 聊天

> 目标：用 GitHub Pages 托管静态聊天界面；通过 Cloudflare Tunnel 安全转发到自己的 Windows 电脑；本机 FastAPI 保存 DeepSeek 密钥并调用模型。此方案适合个人验证和受控试用，不等同于可承载公众流量的生产服务。

## 1. 架构与请求路径

```text
浏览器（GitHub Pages，HTTPS）
   │ HTTPS / JSON 或 SSE
   ▼
Cloudflare 公网入口
   │ Cloudflare Tunnel（由本机主动建立的出站连接）
   ▼
Windows 电脑：cloudflared → 127.0.0.1:8000 FastAPI
   │ HTTPS，服务端持有密钥
   ▼
DeepSeek API
```

浏览器从网页发起请求，FastAPI 校验访问令牌和输入，再向 DeepSeek 的 OpenAI 兼容接口发送消息，最后把回答返回网页。GitHub Pages 只托管前端；它不运行 Python。不要在前端、仓库或浏览器存储中放 DeepSeek API Key。

**关键边界：** 访问令牌放在静态网页中也可被访客查看，因此只能作为个人 MVP 的低门槛访问控制，不能抵抗有意攻击者。若要给不特定公众使用，改用带用户登录的后端/访问代理、配额和监控，最好将后端迁到常驻托管环境。Cloudflare Access 等身份代理可作额外门禁，但需评估其对目标用户的登录体验。

## 2. 前置条件

- Windows 10/11 电脑，安装 Python 3.11+、Git；保持联网。
- GitHub 账号和一个启用 GitHub Pages 的仓库。
- DeepSeek API 账号、可用余额/额度及 API Key。
- Cloudflare 账号。推荐自有域名并接入 Cloudflare，以创建命名 Tunnel 和固定 API 子域名；临时 Quick Tunnel 仅用于短时验证，地址会变化且不适合长期服务。
- 基本命令行操作能力。不要在办公/共享电脑上未经许可运行公开服务。

## 3. 仓库结构

```text
my-deepseek-chat/
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── backend/
│   ├── app.py
│   ├── requirements.txt
│   └── .env.example
├── .gitignore
└── README.md
```

`.gitignore` 至少包含：`.env`、`.venv/`、`__pycache__/`、`.pytest_cache/`。前端发布 `frontend/`；后端代码可放在同一仓库，但 `.env`、密钥和运行日志绝不能提交。公开仓库中的后端代码本身不应含秘密。

## 4. 前端 MVP

页面包含标题/连接状态、对话消息区、输入框、发送按钮、停止按钮和清空对话按钮。支持 Enter 发送、Shift+Enter 换行、加载状态、错误提示、自动滚动及移动端布局。消息历史先保存在浏览器内存，刷新即清空；MVP 不保存敏感对话。

`app.js` 中配置公开的后端 URL（例如 `https://api.example.com`）和个人访问令牌。令牌作为 `Authorization: Bearer …` 请求头发送。此令牌会出现在前端资源中；请把它视为防误触的门槛而非秘密，也不要将网页宣传为开放公共服务。

构造消息时只发送必要的最近若干轮，限制单条输入长度和上下文轮数。对模型输出使用 `textContent` 显示；若要渲染 Markdown，使用可信库并净化 HTML，避免直接把模型内容写入 `innerHTML`。前端从 SSE 接收 `delta` 事件，逐段追加到当前助手消息；连接失败给出可读提示。

## 5. FastAPI 后端

建议端点：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 返回服务状态，不泄露配置或密钥 |
| POST | `/api/chat` | 非流式聊天，返回 `{ "content": "..." }` |
| POST | `/api/chat/stream` | SSE 流式聊天（可选，第二阶段加入） |

请求模型限定角色为 `system/user/assistant`，拒绝客户端伪造工具调用；校验消息数量、角色顺序、字段类型和长度。设定总请求超时、输出 token 上限，并将上游错误映射成不含密钥/堆栈的通用错误。不要记录 Authorization 头或完整对话。

依赖建议：`fastapi`、`uvicorn[standard]`、`openai`、`pydantic-settings`。用虚拟环境安装依赖并锁定实际验证过的版本。启动只绑定回环地址：

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install fastapi "uvicorn[standard]" openai pydantic-settings
uvicorn app:app --host 127.0.0.1 --port 8000
```

### DeepSeek OpenAI 兼容调用

使用官方 DeepSeek API 文档确认当前可用的 `base_url`、模型名、限额及参数。常见接入形式为 Python OpenAI SDK 设置 `base_url="https://api.deepseek.com"`，并将 `api_key` 从环境配置读取；请求 `/chat/completions` 对应的 SDK `chat.completions.create(...)`。模型名不要凭旧教程硬编码，按账户当前可用模型配置。不要把上游 API Key 返回给前端。

配置项建议：`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`APP_BEARER_TOKEN`、`ALLOWED_ORIGINS`、`MAX_MESSAGE_CHARS`、`MAX_HISTORY_MESSAGES`、`UPSTREAM_TIMEOUT_SECONDS`。本地通过 `backend/.env` 加载并确保忽略；生产环境优先 Windows 用户级环境变量或专用密钥管理器。`.env.example` 只放变量名和占位说明，不放真实值。轮换密钥时同时撤销旧 Key。

## 6. CORS、身份认证、限流与安全

- CORS 仅允许精确的 GitHub Pages 源，例如 `https://<账号>.github.io`；若项目站点是子路径，Origin 仍是站点源而非路径。不要使用 `*`，也不要同时启用凭据通配。
- 后端每个聊天端点检查 Bearer Token，使用常量时间比较；未设置令牌时拒绝启动或拒绝请求。注意：静态前端令牌可被提取，不能实现真正的用户身份认证。
- 对每个 IP/令牌做请求频率限制、并发限制、请求体大小限制和模型输出上限。进程内限流在重启后归零，且不能跨多实例；Tunnel 前可再配 Cloudflare WAF/限速规则（依产品计划而定）。设置 DeepSeek 账户预算/告警（如可用），定期查用量。
- 仅让 Uvicorn 监听 `127.0.0.1`；不做路由器端口转发，不开放 Windows 防火墙入站 8000 端口。Tunnel 只路由指定主机名到本地服务。
- 及时更新依赖和 `cloudflared`；不开放 Swagger 文档到公网，或至少确认其不泄露信息。隐藏详细异常，避免 SSRF/任意 URL 代理功能。不要提供文件系统、命令执行或 Agent 工具端点。
- HTTPS 保护浏览器到 Cloudflare 的传输；Tunnel 保护 Cloudflare 到本机的连接。API 令牌和 DeepSeek Key 分别管理。发现泄露立即撤销并换新。

## 7. Cloudflare Tunnel 设置

1. 将自有域名接入 Cloudflare，并确认 DNS/域名状态正常。
2. 在 Cloudflare Zero Trust 控制台创建命名 Tunnel，按控制台当前指引在 Windows 安装并运行 `cloudflared`。安装命令及凭证文件路径以控制台当时显示为准，避免复制旧版教程命令。
3. 为 Tunnel 添加 Public Hostname，例如 `api.example.com`，服务类型 HTTP，目标 `127.0.0.1:8000`。
4. 本机先启动 FastAPI，再启动 Tunnel；从本机检查 `http://127.0.0.1:8000/health`，然后用外网设备访问 `https://api.example.com/health`。
5. Tunnel 凭证属于秘密，限制 Windows 文件权限，不提交仓库，不发给他人。可先在前台运行；确认稳定后再按 Cloudflare 当前 Windows 指引配置后台服务和自动启动。
6. Quick Tunnel 可临时验证，但会产生临时域名、可能受限且地址变化，前端需跟着更新；不要当成稳定生产地址。

## 8. 本地开发与验证

1. 复制 `.env.example` 为 `backend/.env`，填入 API Key、模型名、随机生成的访问令牌和允许的 GitHub Pages Origin。
2. 安装依赖并启动 Uvicorn；查看 `/health`。
3. 用 API 工具向 `/api/chat` 发送一条短消息，确认有回答；确认错误密钥不会泄露细节，缺少/错误令牌被拒绝，超长输入被拒绝。
4. 用本地静态服务器预览前端（不要直接双击文件导致 `file://` Origin 问题），确认 CORS 和聊天交互。
5. 再连接 Tunnel，以手机蜂窝网络访问公网 URL；验证 HTTPS、CORS、令牌、聊天及断线恢复。不要只在同一 Wi-Fi 测试公网链路。

## 9. 发布步骤

1. 将 `frontend/` 推送到 GitHub 私有或公开仓库；在仓库 Settings → Pages 选择发布分支和 `/frontend` 目录（或使用 Actions 将构建产物发布到 Pages）。
2. 等待 GitHub Pages 显示站点地址，检查 HTTPS 与浏览器控制台。
3. 将后端 API 基址设置为 Tunnel 的固定 HTTPS 域名，重新发布前端。
4. 在 Windows 启动 FastAPI 和 Tunnel；确认 `/health` 公网可用，再从异网设备测试完整对话。
5. 建立启动/停止说明、密钥轮换和更新流程；不要把私密测试令牌放在公开 issue、截图或提交记录。

## 10. SSE 流式输出

MVP 可以先做非流式 POST，减少首次排障变量；随后加入 SSE 改善首字响应体验。FastAPI 调用上游时启用 `stream=True`，迭代内容片段并以 `text/event-stream` 返回，事件采用 `data: {JSON}\n\n` 格式，结束发送 `[DONE]` 或定义明确的 `done` 事件。前端使用 `fetch` + `ReadableStream` 解析 SSE，因为原生 `EventSource` 不支持 POST 请求体和自定义 Authorization 头。处理 UTF-8 分块边界、取消请求、客户端断开、上游超时和错误事件；确保及时关闭上游流。不要把 SSE 内容缓存。Cloudflare 对连接时长、缓冲和套餐的行为需按当前产品文档实测。

## 11. 故障排查

| 现象 | 检查 |
|---|---|
| 浏览器 CORS 报错 | `ALLOWED_ORIGINS` 是否精确匹配页面 Origin；预检 OPTIONS 是否由 FastAPI CORS 中间件处理；是否误配 `*`/凭据 |
| 502/无法连接 | FastAPI 是否运行；是否监听 `127.0.0.1:8000`；Tunnel 路由和 hostname 是否指向正确端口 |
| Tunnel 离线 | `cloudflared` 进程、网络、凭证和控制台 Tunnel 状态；Windows 睡眠/休眠是否中断连接 |
| 401 | 前端令牌与后端配置是否一致、是否带 `Bearer ` 前缀；已泄露令牌应直接轮换 |
| DeepSeek 401/余额错误 | 后端环境变量、Key 状态、账户余额/权限、模型名与 API 文档 |
| 请求超时/限流 | 网络、上游状态、输入长度、并发、超时设置和账户限额；避免自动无限重试 |
| SSE 无逐字显示 | 浏览器是否按流读取；代理缓冲/超时；服务端是否逐块 flush，是否错误地缓存响应 |
| 睡眠后不可访问 | Windows 电源设置、登录状态、服务是否自动启动；电脑关机或断网时后端必然不可用 |

排障日志仅记录时间、请求 ID、状态码和耗时；清除密钥、令牌和用户消息。

## 12. 分阶段检查清单

### 阶段 A：本机聊天
- [ ] 创建目录、依赖和 FastAPI `/health`。
- [ ] DeepSeek Key 只保存在本机环境配置。
- [ ] `/api/chat` 完成一轮非流式调用。
- [ ] 鉴权、输入限制、错误处理和精确 CORS 生效。

### 阶段 B：静态网页
- [ ] 完成消息列表、输入框、发送/加载/错误状态。
- [ ] 前端成功调用本机后端；无 XSS 型 HTML 注入。
- [ ] 浏览器不接触 DeepSeek Key。

### 阶段 C：公网隧道
- [ ] 命名 Tunnel 与固定 HTTPS hostname 可用。
- [ ] Uvicorn 仅监听 loopback，无端口转发。
- [ ] 用异网设备验证鉴权、CORS 和对话。

### 阶段 D：GitHub Pages
- [ ] 发布前端并更新后端 URL、Origin 配置。
- [ ] 从 Pages 页面完整聊天成功。
- [ ] 令牌泄露/轮换流程和启动说明已记录。

### 阶段 E：体验增强
- [ ] 加入 SSE、取消生成和断线提示。
- [ ] 加入限流、用量告警、日志脱敏和依赖更新流程。
- [ ] 评估登录、服务常驻和托管后端需求。

## 13. Windows 在线时间与安全提醒

这套架构中，Windows 电脑就是实际后端主机。电脑必须开机、联网、未休眠，FastAPI 和 `cloudflared` 进程都必须运行。关机、重启、断网、系统更新或进程崩溃都会令网页暂时无法聊天；GitHub Pages 仍能打开，但后端不响应。家用电脑的上行带宽、动态网络、供电和散热也影响稳定性。MVP 可手动启动，确认需求后再配置受控的开机启动、进程监控和恢复策略。

公网 Tunnel 会让指定服务可从互联网到达；它不是自动的身份验证。必须坚持 loopback 绑定、最小路由、鉴权、严格限流和密钥隔离。由于前端静态令牌无法保密，不要对陌生公众开放可能产生费用的 DeepSeek 代理。若要长期/多人使用，应采用真正登录与服务端授权、按用户配额和滥用监控，并考虑把后端迁到有监控、备份和稳定运行保障的托管服务。

## 14. 官方资料

开始实施时请以官方最新文档核对命令、模型和产品限制：

- [GitHub Pages 文档](https://docs.github.com/pages)
- [Cloudflare Tunnel 文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
- [Cloudflare Tunnel Windows 安装](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/windows/)
- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [DeepSeek API 文档](https://api-docs.deepseek.com/)
