"""个人 DeepSeek 聊天 MVP 后端。

在同一进程内托管聊天 API 与静态前端(同源访问,无 CORS 问题):
- 仅监听 127.0.0.1,由 Cloudflare Tunnel 对外暴露
- Bearer Token 鉴权、按 IP 限流、并发与输入限制
- 日志不含 Authorization 头与对话内容
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Literal

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("chatpage")

BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"


class Settings(BaseSettings):
    """从 backend/.env 与环境变量读取配置。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    app_bearer_token: str = ""
    allowed_origins: str = ""  # 逗号分隔的额外允许来源;同源托管时无需配置
    max_message_chars: int = 4000
    max_history_messages: int = 20
    max_output_tokens: int = 2048
    upstream_timeout_seconds: float = 60.0
    rate_limit_per_minute: int = 30
    max_concurrent_requests: int = 2
    max_request_bytes: int = 256 * 1024


settings = Settings()

if not settings.app_bearer_token or len(settings.app_bearer_token) < 16:
    raise RuntimeError(
        "APP_BEARER_TOKEN 未配置或长度不足 16 字符,请在 backend/.env 中设置后重启。"
    )

app = FastAPI(title="chatpage", docs_url=None, redoc_url=None, openapi_url=None)

# 同源托管时无需 CORS;仅当配置了额外来源时才启用,且不使用通配符
_extra_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
if _extra_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_extra_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


class RateLimiter:
    """按 key 的固定窗口限流。进程内实现,重启后计数归零。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._hits) > 10_000:
            # 防止 key 集合无限增长
            self._hits = {
                k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window
            }
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


chat_limiter = RateLimiter(settings.rate_limit_per_minute, 60.0)
upstream_slots = asyncio.Semaphore(settings.max_concurrent_requests)


@app.middleware("http")
async def guard_and_log(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > settings.max_request_bytes
    ):
        return JSONResponse(status_code=413, content={"detail": "请求体过大"})

    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    ip = request.client.host if request.client else "-"
    logger.info(
        "req=%s ip=%s method=%s path=%s status=%s dur_ms=%.1f",
        request_id,
        ip,
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    Tunnel 场景下 request.client.host 是本机 cloudflared(127.0.0.1),
    真实 IP 由 Cloudflare 边缘写入 X-Forwarded-For 且无法被客户端伪造。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_token(request: Request) -> None:
    header = request.headers.get("authorization", "")
    expected = settings.app_bearer_token
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="访问令牌缺失")
    provided = header.removeprefix("Bearer ")
    # 字节级常量时间比较;长度不同或内容不同都安全地返回 False
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="访问令牌无效")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 拒绝 tool_calls 等伪造字段

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=settings.max_message_chars)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息内容不能为空")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(
        min_length=1, max_length=settings.max_history_messages
    )


class ChatResponse(BaseModel):
    content: str


def validate_conversation(messages: list[ChatMessage]) -> None:
    """校验角色顺序:system 只允许在开头,最后必须是 user,assistant 不能连续出现。

    允许连续 user 消息:前端在失败时可能移除消息、历史截断后可能以 assistant 开头,
    过于严格的交替校验会在这些情况下误伤正常请求。
    """
    if messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="最后一条消息必须是用户消息")
    for index, message in enumerate(messages):
        if message.role == "system" and index != 0:
            raise HTTPException(status_code=400, detail="system 消息只允许出现在开头")
        if (
            message.role == "assistant"
            and index > 0
            and messages[index - 1].role == "assistant"
        ):
            raise HTTPException(status_code=400, detail="消息角色顺序无效")


client = AsyncOpenAI(
    api_key=settings.deepseek_api_key or "unset",
    base_url=settings.deepseek_base_url,
    timeout=settings.upstream_timeout_seconds,
    max_retries=1,
)


async def call_deepseek(messages: list[dict[str, str]]) -> str:
    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=messages,
        max_tokens=settings.max_output_tokens,
        stream=False,
    )
    return response.choices[0].message.content or ""


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查,不泄露任何配置信息。"""
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    ip = client_ip(request)
    require_token(request)

    if not chat_limiter.allow(ip):
        raise HTTPException(status_code=429, detail="请求过于频繁,请稍后再试")
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=503, detail="服务端未配置 DeepSeek API Key")
    validate_conversation(payload.messages)

    if upstream_slots.locked():
        raise HTTPException(status_code=429, detail="当前并发请求过多,请稍后再试")

    try:
        async with upstream_slots:
            content = await call_deepseek([m.model_dump() for m in payload.messages])
    except openai.APITimeoutError:
        logger.warning("上游超时 ip=%s", ip)
        raise HTTPException(status_code=504, detail="上游响应超时,请稍后再试")
    except openai.APIConnectionError:
        logger.warning("上游连接失败 ip=%s", ip)
        raise HTTPException(status_code=502, detail="上游服务暂时不可用,请稍后再试")
    except openai.APIError as exc:
        # 只记录错误类型与状态码,不记录可能包含请求内容的消息体
        status_code = getattr(exc, "status_code", None)
        logger.warning("上游错误 ip=%s type=%s status=%s", ip, type(exc).__name__, status_code)
        raise HTTPException(status_code=502, detail="上游服务暂时不可用,请稍后再试")
    except Exception:
        logger.exception("未预期错误 ip=%s", ip)
        raise HTTPException(status_code=500, detail="服务内部错误")

    return ChatResponse(content=content)


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    ip = client_ip(request)
    require_token(request)

    if not chat_limiter.allow(ip):
        raise HTTPException(status_code=429, detail="请求过于频繁,请稍后再试")
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=503, detail="服务端未配置 DeepSeek API Key")
    validate_conversation(payload.messages)

    if upstream_slots.locked():
        raise HTTPException(status_code=429, detail="当前并发请求过多,请稍后再试")

    async def event_stream():
        # 流开始后无法再改 HTTP 状态码,错误通过 error 事件传给前端
        try:
            async with upstream_slots:
                stream = await client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=[m.model_dump() for m in payload.messages],
                    max_tokens=settings.max_output_tokens,
                    stream=True,
                )
                async with stream:
                    async for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        piece = getattr(delta, "content", None)
                        if piece:
                            yield (
                                "data: "
                                + json.dumps({"delta": piece}, ensure_ascii=False)
                                + "\n\n"
                            )
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # 客户端断开:退出生成器,async with 会随之关闭上游流
            logger.info("客户端断开,停止上游流 ip=%s", ip)
            raise
        except openai.APITimeoutError:
            logger.warning("流式上游超时 ip=%s", ip)
            yield "data: " + json.dumps({"error": "上游响应超时,请稍后再试"}, ensure_ascii=False) + "\n\n"
        except openai.APIError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.warning("流式上游错误 ip=%s type=%s status=%s", ip, type(exc).__name__, status_code)
            yield "data: " + json.dumps({"error": "上游服务暂时不可用,请稍后再试"}, ensure_ascii=False) + "\n\n"
        except Exception:
            logger.exception("流式未预期错误 ip=%s", ip)
            yield "data: " + json.dumps({"error": "服务内部错误"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning("未找到前端目录,仅提供 API: %s", FRONTEND_DIR)
