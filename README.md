# chatpage — 个人 DeepSeek 聊天 MVP

《github-pages-fastapi-deepseek-mvp-zh.md》方案的个人简化版:
FastAPI 同源托管聊天 API 与静态前端,本机经 Cloudflare Quick Tunnel 对外暴露,
手机 / 其他电脑用浏览器直接访问。仅适合个人使用,不要作为公共服务。

## 结构

- `backend/` — FastAPI 后端(聊天 API + 静态前端托管)
- `frontend/` — 静态聊天页面
- `github-pages-fastapi-deepseek-mvp-zh.md` — 完整方案文档(安全说明、Tunnel 配置、故障排查)

## 快速开始(uv)

```powershell
# 1. 生成访问令牌(输出复制备用)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. 复制配置模板,填入 DEEPSEEK_API_KEY 与 APP_BEARER_TOKEN
copy backend\.env.example backend\.env

# 3. 安装依赖
cd backend
uv venv .venv
uv pip install -r requirements.txt

# 4. 启动(只监听回环地址)
uv run uvicorn app:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>,首次会要求粘贴访问令牌(保存在浏览器 localStorage)。
后端未配置 DeepSeek Key 时页面可正常打开,但聊天会提示 503。

## 公网访问

按方案文档第 7 节使用 Cloudflare Quick Tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

把生成的 `https://xxxx.trycloudflare.com` 地址加入手机 / 其他电脑的书签即可。
Quick Tunnel 每次重启地址会变,换地址只影响书签,不影响代码。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查,无鉴权 |
| POST | `/api/chat` | 聊天,`Authorization: Bearer <令牌>`,非流式 |
| POST | `/api/chat/stream` | 流式聊天(SSE),前端使用此端点 |

## 安全要点

- 只监听 `127.0.0.1`,不做端口转发、不开防火墙入站
- 令牌为至少 16 字符随机值;泄露立即更换(改 `.env` 并重启)
- 按 IP 限流(默认 30 次/分钟)、并发上限(默认 2)、请求体 / 单条消息 / 历史条数限制
- 日志不含令牌与对话内容;Swagger 文档已关闭
- 隧道地址与令牌不要发给他人
