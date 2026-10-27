from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from qstash.receiver import SignatureError
from telegram import Update
from telegram.error import TelegramError
from upstash_redis.errors import UpstashError

from .config import Settings, get_settings
from .service import Runtime, handle_timeout, handle_update

LOGGER = logging.getLogger("telegram_guard_bot.web")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime = Runtime(settings)
    await runtime.startup()
    app.state.runtime = runtime
    LOGGER.info("云端机器人已初始化 @%s", runtime.bot.username)
    try:
        yield
    finally:
        await runtime.shutdown()


app = FastAPI(
    title="Telegram Guard Bot",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def configured_or_error(request: Request) -> Runtime | None:
    try:
        return runtime(request)
    except (AttributeError, RuntimeError):
        return None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    ready = configured_or_error(request) is not None
    status = "运行中" if ready else "配置不完整"
    detail = (
        "Telegram Webhook、Redis 会话和 QStash 超时任务已就绪。"
        if ready
        else "请在 Vercel 配置全部必需环境变量后重新部署。详细错误请查看 Functions 日志。"
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Telegram Guard Bot</title><body style='font-family:sans-serif;max-width:680px;margin:64px auto;padding:24px'>"
        f"<h1>🛡️ Telegram Guard Bot</h1><p><strong>{status}</strong></p><p>{detail}</p>"
        "<p>机器人不通过本网页登录；请从验证群的按钮进入。</p></body></html>"
    )


@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    active = configured_or_error(request)
    if active is None:
        return JSONResponse({"ok": False, "error": "configuration_or_startup_error"}, status_code=503)
    return JSONResponse({"ok": True, "bot": active.bot.username})


def require_admin_secret(request: Request, supplied: str | None) -> None:
    settings: Settings = runtime(request).settings
    if not supplied or not hmac.compare_digest(supplied, settings.admin_secret):
        raise HTTPException(status_code=401, detail="无效管理密钥")


@app.post("/api/setup")
async def setup_webhook(request: Request) -> dict[str, object]:
    require_admin_secret(request, request.headers.get("X-Admin-Setup-Secret"))
    active = runtime(request)
    try:
        await active.bot.set_webhook(
            url=active.settings.telegram_webhook_url,
            secret_token=active.settings.webhook_secret,
            allowed_updates=["chat_member", "message", "callback_query"],
            drop_pending_updates=False,
            max_connections=20,
        )
        await active.store.redis.ping()
        return {"ok": True, "webhook_url": active.settings.telegram_webhook_url}
    except (TelegramError, UpstashError) as exc:
        LOGGER.exception("Webhook/Redis 设置检查失败")
        raise HTTPException(status_code=502, detail="Telegram 或 Redis 设置失败，请查看日志") from exc


@app.get("/api/setup/status")
async def setup_status(request: Request) -> dict[str, object]:
    require_admin_secret(request, request.headers.get("X-Admin-Setup-Secret"))
    active = runtime(request)
    info = await active.bot.get_webhook_info()
    return {
        "url": info.url,
        "expected_url": active.settings.telegram_webhook_url,
        "matches": info.url == active.settings.telegram_webhook_url,
        "pending_update_count": info.pending_update_count,
        "last_error_message": info.last_error_message,
    }


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> Response:
    active = runtime(request)
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, active.settings.webhook_secret):
        raise HTTPException(status_code=401, detail="无效 Webhook 密钥")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="请求过大")
    try:
        update = Update.de_json(json.loads(raw), active.bot)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="无效 Telegram Update") from exc
    if update is None:
        return Response(status_code=200)
    update_type = next((
        name for name in (
            "chat_member", "my_chat_member", "message", "callback_query",
            "chat_join_request",
        ) if getattr(update, name, None) is not None
    ), "other")
    chat = update.effective_chat
    LOGGER.info(
        "收到 Telegram Update update_id=%s type=%s chat_id=%s",
        update.update_id, update_type, chat.id if chat else None,
    )
    try:
        await handle_update(update, active)
    except Exception:
        LOGGER.exception("Telegram Update 处理失败 update_id=%s", update.update_id)
        raise HTTPException(status_code=503, detail="临时处理失败")
    return Response(status_code=200)


@app.post("/api/timeout")
async def timeout_webhook(request: Request) -> Response:
    active = runtime(request)
    raw = await request.body()
    signature = request.headers.get("Upstash-Signature", "")
    try:
        active.receiver.verify(
            signature=signature,
            body=raw.decode("utf-8"),
            url=active.settings.timeout_webhook_url,
        )
    except (SignatureError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="无效 QStash 签名") from exc
    try:
        payload = json.loads(raw)
        token = payload["token"]
        if not isinstance(token, str) or len(token) > 100:
            raise ValueError("invalid token")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="无效超时任务") from exc
    try:
        result = await handle_timeout(token, active)
    except Exception:
        LOGGER.exception("QStash 超时任务处理失败")
        raise HTTPException(status_code=503, detail="临时处理失败，QStash 将重试")
    return JSONResponse({"ok": result != "error", "result": result})


@app.exception_handler(RuntimeError)
async def configuration_error(_: Request, exc: RuntimeError) -> PlainTextResponse:
    LOGGER.error("启动/配置错误: %s", exc)
    return PlainTextResponse("服务配置错误，请查看 Functions 日志", status_code=503)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("guard.web:app", host="127.0.0.1", port=3000, reload=False)
