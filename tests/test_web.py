from __future__ import annotations

import hmac
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guard.web import telegram_webhook, timeout_webhook, setup_webhook
from test_cloud import make_runtime, settings


def test_telegram_webhook_rejects_wrong_secret_and_accepts_valid_update():
    runtime = SimpleNamespace(settings=settings(), bot=SimpleNamespace(username="guard_test_bot"))
    test_app = FastAPI()
    test_app.state.runtime = runtime
    test_app.post("/api/telegram")(telegram_webhook)
    update = {"update_id": 1}
    with TestClient(test_app, raise_server_exceptions=True) as client:
        assert client.post("/api/telegram", json=update).status_code == 401
        assert client.post(
            "/api/telegram", json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": settings().webhook_secret},
        ).status_code == 200


def test_timeout_requires_qstash_signature_before_running_handler(monkeypatch):
    runtime = SimpleNamespace(settings=settings(), receiver=Mock())
    runtime.receiver.verify = Mock(side_effect=ValueError("bad signature"))
    test_app = FastAPI()
    test_app.state.runtime = runtime
    test_app.post("/api/timeout")(timeout_webhook)
    import guard.web as web_module
    handler = AsyncMock()
    original = web_module.handle_timeout
    web_module.handle_timeout = handler
    with TestClient(test_app, raise_server_exceptions=True) as client:
        response = client.post("/api/timeout", json={"token": "token"}, headers={"Upstash-Signature": "bad"})
    assert response.status_code == 401
    handler.assert_not_awaited()
    web_module.handle_timeout = original


def test_setup_endpoint_is_protected(monkeypatch):
    runtime = SimpleNamespace(settings=settings(), bot=SimpleNamespace(set_webhook=AsyncMock(return_value=True)), store=SimpleNamespace(redis=SimpleNamespace(ping=AsyncMock(return_value="PONG"))))
    test_app = FastAPI()
    test_app.state.runtime = runtime
    test_app.post("/api/setup")(setup_webhook)
    with TestClient(test_app, raise_server_exceptions=True) as client:
        assert client.post("/api/setup").status_code == 401
        response = client.post("/api/setup", headers={"X-Admin-Setup-Secret": settings().admin_secret})
    assert response.status_code == 200
    runtime.bot.set_webhook.assert_awaited_once()
    assert runtime.bot.set_webhook.await_args.kwargs["secret_token"] == settings().webhook_secret
    assert "chat_member" in runtime.bot.set_webhook.await_args.kwargs["allowed_updates"]
import base64
import hashlib
import time

import jwt
import pytest
from qstash import Receiver
from qstash.receiver import SignatureError


def test_real_qstash_jwt_validates_body_url_and_rejects_tampering():
    body = '{"token":"abc"}'
    url = "https://bot.example.com/api/timeout"
    now = int(time.time())
    digest = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).decode().rstrip("=")
    signature = jwt.encode(
        {"iss": "Upstash", "sub": url, "body": digest, "nbf": now - 5, "exp": now + 60},
        "current", algorithm="HS256",
    )
    receiver = Receiver(current_signing_key="current", next_signing_key="next")
    receiver.verify(signature=signature, body=body, url=url)
    with pytest.raises(SignatureError):
        receiver.verify(signature=signature, body='{"token":"other"}', url=url)
    with pytest.raises(SignatureError):
        receiver.verify(signature=signature, body=body, url="https://other.example.com/api/timeout")
    with pytest.raises(SignatureError):
        Receiver(current_signing_key="wrong", next_signing_key="also-wrong").verify(
            signature=signature, body=body, url=url,
        )
