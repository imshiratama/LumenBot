from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _ids(name: str) -> frozenset[int]:
    values = re.split(r"[\s,]+", os.getenv(name, "").strip())
    try:
        return frozenset(int(value) for value in values if value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 只能包含数字 ID，用逗号分隔") from exc


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0")
    return value


def public_base_url() -> str:
    """Use a stable production domain for delayed QStash callbacks."""
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        if not configured.startswith("https://") or "/" in configured[8:]:
            raise RuntimeError("PUBLIC_BASE_URL 必须是 https://域名，且不能包含路径")
        return configured
    domain = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    if domain:
        return f"https://{domain}"
    if os.getenv("VERCEL"):
        raise RuntimeError("未能确定生产域名，请设置 PUBLIC_BASE_URL")
    return "http://127.0.0.1:3000"


@dataclass(frozen=True, slots=True)
class Settings:
    tg_token: str
    target_x_username: str
    twitterapi_key: str
    admin_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    webhook_secret: str
    admin_secret: str
    redis_url: str
    redis_token: str
    qstash_token: str
    qstash_current_signing_key: str
    qstash_next_signing_key: str
    base_url: str
    timeout_seconds: int = 300
    max_x_attempts: int = 5
    x_retry_seconds: int = 10
    x_request_timeout: float = 15.0
    fail_open: bool = False

    @property
    def x_profile_url(self) -> str:
        return f"https://x.com/{self.target_x_username}"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.base_url}/api/telegram"

    @property
    def timeout_webhook_url(self) -> str:
        return f"{self.base_url}/api/timeout"


def get_settings() -> Settings:
    target = os.getenv("TARGET_X_ID", "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", target) is None:
        raise RuntimeError("TARGET_X_ID 必须是 1-15 位 X 用户名")
    secret_pattern = re.compile(r"[A-Za-z0-9_-]{16,256}")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    admin_secret = os.getenv("ADMIN_SETUP_SECRET", "").strip()
    if secret_pattern.fullmatch(webhook_secret) is None:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET 需为 16-256 位字母、数字、_ 或 -")
    if secret_pattern.fullmatch(admin_secret) is None:
        raise RuntimeError("ADMIN_SETUP_SECRET 需为 16-256 位字母、数字、_ 或 -")
    fail_open = os.getenv("X_API_FAIL_OPEN", "false").lower() in {"1", "true", "yes", "on"}
    admins = _ids("ADMIN_IDS")
    if not fail_open and not admins:
        raise RuntimeError("默认人工降级需要配置 ADMIN_IDS")
    required = {
        "TG_TOKEN": os.getenv("TG_TOKEN", "").strip(),
        "TWITTERAPI_KEY": os.getenv("TWITTERAPI_KEY", "").strip(),
        "UPSTASH_REDIS_REST_URL": os.getenv("UPSTASH_REDIS_REST_URL", "").strip(),
        "UPSTASH_REDIS_REST_TOKEN": os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip(),
        "QSTASH_TOKEN": os.getenv("QSTASH_TOKEN", "").strip(),
        "QSTASH_CURRENT_SIGNING_KEY": os.getenv("QSTASH_CURRENT_SIGNING_KEY", "").strip(),
        "QSTASH_NEXT_SIGNING_KEY": os.getenv("QSTASH_NEXT_SIGNING_KEY", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("缺少环境变量：" + ", ".join(missing))
    return Settings(
        tg_token=required["TG_TOKEN"],
        target_x_username=target,
        twitterapi_key=required["TWITTERAPI_KEY"],
        admin_ids=admins,
        allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
        webhook_secret=webhook_secret,
        admin_secret=admin_secret,
        redis_url=required["UPSTASH_REDIS_REST_URL"],
        redis_token=required["UPSTASH_REDIS_REST_TOKEN"],
        qstash_token=required["QSTASH_TOKEN"],
        qstash_current_signing_key=required["QSTASH_CURRENT_SIGNING_KEY"],
        qstash_next_signing_key=required["QSTASH_NEXT_SIGNING_KEY"],
        base_url=public_base_url(),
        timeout_seconds=_positive_int("VERIFICATION_TIMEOUT_SECONDS", 300),
        max_x_attempts=_positive_int("MAX_X_ATTEMPTS", 5),
        x_retry_seconds=_positive_int("X_RETRY_SECONDS", 10),
        x_request_timeout=float(os.getenv("X_REQUEST_TIMEOUT_SECONDS", "15")),
        fail_open=fail_open,
    )
