from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guard import service
from guard.config import Settings
from guard.service import Runtime
from guard.store import (
    ADD_SCRIPT, CLAIM_EXPIRED_SCRIPT, REMOVE_SCRIPT, RESTORE_SCRIPT, STATE_SCRIPT, Session, SessionStore,
)


def settings(**overrides) -> Settings:
    values = dict(
        tg_token="123456:TEST", target_x_username="TargetX", twitterapi_key="x-key",
        allowed_chat_ids=frozenset({-100123}),
        webhook_secret="webhook-secret-123456", admin_secret="admin-secret-123456",
        redis_url="https://redis.invalid", redis_token="redis-secret", qstash_token="qstash-secret",
        qstash_current_signing_key="current", qstash_next_signing_key="next",
        base_url="https://bot.example.com", timeout_seconds=300, max_x_attempts=5,
        x_retry_seconds=0, x_request_timeout=15.0, fail_open=False,
    )
    values.update(overrides)
    return Settings(**values)


class MemoryRedis:
    """Deterministic test double; production uses the exact Lua scripts in store.py."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value, **kwargs):
        self.data[key] = str(value)
        return True

    async def eval(self, script: str, keys=None, args=None):
        keys, args = keys or [], list(args or [])
        if script == ADD_SCRIPT:
            token_key, member_key, prefix = keys
            old_token = self.data.get(member_key)
            if old_token:
                old = self.data.get(prefix + old_token)
                if old and float(args[0]) < Session.from_json(old).expires_at:
                    return "duplicate"
                self.data.pop(prefix + old_token, None)
            self.data[token_key] = args[1]
            self.data[member_key] = args[3]
            return "ok"
        if script == STATE_SCRIPT:
            raw = self.data.get(keys[0])
            if not raw:
                return None
            session = Session.from_json(raw)
            if float(args[0]) >= session.expires_at:
                return None
            action = args[1]
            if action == "read":
                return raw
            if action == "claim":
                if session.stage not in {"checking", "review", "manual_approving"}:
                    return None
                session.stage = "approving"
            elif action in {"manual_approve", "reject"}:
                if session.chat_id != int(args[2]):
                    return None
                if session.stage not in {"new", "ready", "awaiting_x", "checking", "review"}:
                    return None
                session.stage = "manual_approving" if action == "manual_approve" else "rejecting"
            elif action == "stage":
                if session.stage != args[2]:
                    return None
                session.stage = args[3]
            elif action == "begin_x":
                if session.stage != "awaiting_x" or session.user_id != int(args[2]):
                    return None
                if session.attempts >= int(args[3]):
                    return "limit"
                if float(args[0]) - session.last_attempt_at < float(args[4]):
                    return "rate_limited"
                session.stage, session.x_username = "checking", args[5]
                session.attempts += 1
                session.last_attempt_at = float(args[0])
            elif action == "message":
                setattr(session, f"{args[3]}_message_id", int(args[2]))
            self.data[keys[0]] = session.to_json()
            return self.data[keys[0]]
        if script in {CLAIM_EXPIRED_SCRIPT, REMOVE_SCRIPT}:
            raw = self.data.get(keys[0])
            if not raw:
                return None
            session = Session.from_json(raw)
            if script == CLAIM_EXPIRED_SCRIPT and float(args[0]) < session.expires_at:
                return None
            if script == REMOVE_SCRIPT and args[0] and session.stage != args[0]:
                return None
            self.data.pop(keys[0], None)
            if self.data.get(keys[1]) == args[-1]:
                self.data.pop(keys[1], None)
            return raw
        if script == RESTORE_SCRIPT:
            if keys[0] in self.data:
                return "exists"
            if self.data.get(keys[1]) not in {None, args[4]}:
                return "replaced"
            if float(args[0]) >= float(args[2]):
                return "expired"
            self.data[keys[0]] = args[1]
            self.data[keys[1]] = args[4]
            return "ok"
        if script.startswith("if redis.call('GET', KEYS[1]) == ARGV[1]"):
            if self.data.get(keys[0]) == args[0]:
                self.data.pop(keys[0], None)
                return 1
            return 0
        raise AssertionError("Unexpected Lua script")

    async def close(self):
        pass


async def make_runtime(config: Settings | None = None) -> Runtime:
    runtime = Runtime.__new__(Runtime)
    runtime.settings = config or settings()
    runtime.store = SessionStore(MemoryRedis())
    runtime.qstash = SimpleNamespace(message=SimpleNamespace(publish_json=AsyncMock()))
    runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    runtime.receiver = Mock()
    runtime.bot = SimpleNamespace(
        username="guard_test_bot",
        restrict_chat_member=AsyncMock(return_value=True),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="restricted", is_member=True)),
        ban_chat_member=AsyncMock(return_value=True),
        unban_chat_member=AsyncMock(return_value=True),
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=1)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        edit_message_text=AsyncMock(return_value=True),
        delete_message=AsyncMock(return_value=True),
    )
    return runtime


async def pending(runtime: Runtime, stage: str = "checking", token: str = "token", chat_id: int = -100123) -> Session:
    now = time.time()
    session = Session(token=token, chat_id=chat_id, user_id=123, created_at=now, expires_at=now + 300, stage=stage, x_username="UserA")
    assert await runtime.store.add(session)
    return session


def member_update(user_id: int = 123):
    return SimpleNamespace(chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        old_chat_member=SimpleNamespace(status="left", is_member=False),
        new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=user_id, is_bot=False, full_name="User")),
    ))


def message_update(text: str):
    return SimpleNamespace(effective_message=SimpleNamespace(
        text=text, chat=SimpleNamespace(type="private"), reply_text=AsyncMock(), entities=[],
    ), effective_user=SimpleNamespace(id=123, first_name="User"))


def review_update(data: str = "ma:token", user_id: int = 999, chat_id: int = -100123):
    return SimpleNamespace(callback_query=SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        answer=AsyncMock(),
    ))


@pytest.mark.asyncio
async def test_join_schedules_durable_timeout_after_muting():
    runtime = await make_runtime()
    await service.on_chat_member(member_update(), runtime)
    runtime.bot.restrict_chat_member.assert_awaited_once()
    runtime.qstash.message.publish_json.assert_awaited_once()
    task = runtime.qstash.message.publish_json.await_args.kwargs
    assert task["delay"] == 300
    assert task["url"] == "https://bot.example.com/api/timeout"
    assert len(task["body"]["token"]) == 22
    runtime.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_join_does_not_schedule_second_timeout():
    runtime = await make_runtime()
    await service.on_chat_member(member_update(), runtime)
    await service.on_chat_member(member_update(), runtime)
    assert runtime.qstash.message.publish_json.await_count == 1
    assert runtime.bot.restrict_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_join_logs_real_chat_id_when_whitelist_rejects(caplog):
    runtime = await make_runtime(settings(allowed_chat_ids=frozenset({-100999})))
    with caplog.at_level("WARNING", logger="telegram_guard_bot.cloud"):
        await service.on_chat_member(member_update(), runtime)
    assert "chat_id=-100123" in caplog.text
    assert "ALLOWED_CHAT_IDS" in caplog.text
    runtime.bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_qstash_publish_failure_is_visible_and_does_not_fake_verification():
    runtime = await make_runtime()
    runtime.qstash.message.publish_json.side_effect = RuntimeError("QStash unavailable")
    await service.on_chat_member(member_update(), runtime)
    alert = runtime.bot.send_message.await_args.kwargs
    assert "无法创建超时任务" in alert["text"]
    assert await runtime.store.for_user(-100123, 123) is None


@pytest.mark.asyncio
async def test_private_start_and_x_success_complete_flow():
    runtime = await make_runtime()
    session = await pending(runtime, "ready")
    update = message_update("")
    await service.on_start(update, runtime, ["verify_token"])
    assert await runtime.store.private_token(123) == "token"
    assert (await runtime.store.get("token")).stage == "awaiting_x"
    await runtime.http.aclose()
    runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"status": "success", "data": {"following": True}},
    )))
    await service.on_x_username(message_update("@UserA"), runtime)
    runtime.bot.restrict_chat_member.assert_awaited_once()
    assert await runtime.store.get("token") is None
    assert await runtime.store.private_token(123) is None
    await runtime.http.aclose()


@pytest.mark.asyncio
async def test_x_failure_defaults_to_manual_review():
    runtime = await make_runtime()
    await pending(runtime, "awaiting_x")
    await runtime.store.set_private(123, "token")
    await service.on_x_username(message_update("UserA"), runtime)
    session = await runtime.store.get("token")
    assert session.stage == "review"
    runtime.bot.restrict_chat_member.assert_not_awaited()
    assert runtime.bot.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_fail_open_only_happens_when_explicit():
    runtime = await make_runtime(settings(fail_open=True))
    await pending(runtime, "awaiting_x")
    await runtime.store.set_private(123, "token")
    await service.on_x_username(message_update("UserA"), runtime)
    runtime.bot.restrict_chat_member.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("following", [True, False])
async def test_follow_result_is_strict_boolean(following):
    runtime = await make_runtime()
    await runtime.http.aclose()
    runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"status": "success", "data": {"following": following}},
    )))
    assert await service.check_x_follow(runtime, "UserA") is following
    await runtime.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status": "success", "data": {"following": "true"}},
    {"status": "success", "data": {"following": 1}},
    {"status": "error", "message": "insufficient credits"},
])
async def test_bad_x_responses_never_prove_follow(payload):
    runtime = await make_runtime()
    await runtime.http.aclose()
    runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(service.XServiceUnavailable):
        await service.check_x_follow(runtime, "UserA")
    await runtime.http.aclose()


@pytest.mark.asyncio
async def test_timeout_requires_actual_expiry_and_is_idempotent():
    runtime = await make_runtime()
    session = await pending(runtime)
    assert await service.handle_timeout("token", runtime) == "not_expired"
    runtime.bot.ban_chat_member.assert_not_awaited()
    await runtime.store.remove("token")
    session.expires_at = time.time() - 1
    runtime.store.redis.data[runtime.store._key("token")] = session.to_json()
    assert await service.handle_timeout("token", runtime) == "kicked"
    runtime.bot.ban_chat_member.assert_awaited_once()
    runtime.bot.unban_chat_member.assert_awaited_once()
    assert await service.handle_timeout("token", runtime) == "not_expired"
    assert runtime.bot.ban_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_timeout_does_not_kick_member_admin_already_unmuted():
    runtime = await make_runtime()
    session = await pending(runtime)
    await runtime.store.remove("token")
    session.expires_at = time.time() - 1
    runtime.store.redis.data[runtime.store._key("token")] = session.to_json()
    runtime.bot.get_chat_member.return_value = SimpleNamespace(status="member")
    assert await service.handle_timeout("token", runtime) == "already_unrestricted"
    runtime.bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_timeout_does_not_remove_new_member_index():
    runtime = await make_runtime()
    old = await pending(runtime, token="old")
    await runtime.store.remove("old")
    old.expires_at = time.time() - 1
    runtime.store.redis.data[runtime.store._key("old")] = old.to_json()
    new = await pending(runtime, token="new")
    await runtime.store.expire("old")
    assert await runtime.store.for_user(-100123, 123) is not None
    assert (await runtime.store.for_user(-100123, 123)).token == "new"


@pytest.mark.asyncio
async def test_join_card_has_group_admin_action_row():
    runtime = await make_runtime()
    await service.on_chat_member(member_update(), runtime)
    markup = runtime.bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[2]] == ["✅ 管理员批准", "❌ 管理员拒绝"]
    token = runtime.qstash.message.publish_json.await_args.kwargs["body"]["token"]
    assert [button.callback_data for button in markup.inline_keyboard[2]] == [f"ma:{token}", f"mr:{token}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["administrator", "creator"])
@pytest.mark.parametrize("action", ["ma", "mr"])
async def test_current_group_admins_can_approve_or_reject(status, action):
    runtime = await make_runtime()
    await pending(runtime, "new")
    runtime.bot.get_chat_member.return_value = SimpleNamespace(status=status)
    update = review_update(f"{action}:token")
    await service.on_manual_review(update, runtime)
    runtime.bot.get_chat_member.assert_any_await(-100123, 999)
    if action == "mr":
        runtime.bot.get_chat_member.assert_awaited_with(-100123, 123)
    if action == "ma":
        runtime.bot.restrict_chat_member.assert_awaited_once()
        runtime.bot.ban_chat_member.assert_not_awaited()
    else:
        runtime.bot.ban_chat_member.assert_awaited_once()
        runtime.bot.unban_chat_member.assert_awaited_once()
        runtime.bot.restrict_chat_member.assert_not_awaited()
    assert await runtime.store.get("token") is None


@pytest.mark.asyncio
async def test_regular_member_cannot_use_admin_buttons():
    runtime = await make_runtime()
    await pending(runtime, "review")
    runtime.bot.get_chat_member.return_value = SimpleNamespace(status="member")
    update = review_update()
    await service.on_manual_review(update, runtime)
    assert (await runtime.store.get("token")).stage == "review"
    runtime.bot.restrict_chat_member.assert_not_awaited()
    runtime.bot.ban_chat_member.assert_not_awaited()
    assert "只有本群当前管理员" in update.callback_query.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_admin_identity_lookup_failure_fails_closed():
    from telegram.error import TelegramError
    runtime = await make_runtime()
    await pending(runtime, "review")
    runtime.bot.get_chat_member.side_effect = TelegramError("temporary failure")
    update = review_update()
    await service.on_manual_review(update, runtime)
    assert (await runtime.store.get("token")).stage == "review"
    runtime.bot.restrict_chat_member.assert_not_awaited()
    assert "暂时无法确认" in update.callback_query.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cross_group_admin_callback_cannot_act():
    runtime = await make_runtime(settings(allowed_chat_ids=frozenset({-100123, -100999})))
    await pending(runtime, "review", chat_id=-100999)
    runtime.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    await service.on_manual_review(review_update(), runtime)
    assert (await runtime.store.get("token")).stage == "review"
    runtime.bot.restrict_chat_member.assert_not_awaited()
    runtime.bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_during_x_check_prevents_later_approval():
    runtime = await make_runtime()
    await pending(runtime, "checking")
    runtime.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    await service.on_manual_review(review_update("mr:token"), runtime)
    assert await service.approve(runtime, "token") == service.CompletionResult.EXPIRED
    runtime.bot.restrict_chat_member.assert_not_awaited()
    assert await runtime.store.get("token") is None


def test_lua_scripts_have_atomic_guards_and_expected_keys():
    assert "if now >= s.expires_at then return nil end" in STATE_SCRIPT
    assert "if s.stage ~= ARGV[3] then return nil end" in STATE_SCRIPT
    assert "if tostring(s.chat_id) ~= ARGV[3] then return nil end" in STATE_SCRIPT
    assert "s.stage = 'manual_approving'" in STATE_SCRIPT
    assert "s.stage = 'rejecting'" in STATE_SCRIPT
    assert "redis.call('GET', KEYS[2]) == ARGV[2]" in CLAIM_EXPIRED_SCRIPT
    assert "redis.call('GET', KEYS[2]) == ARGV[2]" in REMOVE_SCRIPT
    assert "owner ~= ARGV[5]" in RESTORE_SCRIPT
    assert "cjson.decode(ARGV[2])" not in ADD_SCRIPT


def test_telegram_update_parser_matches_real_ptb_objects():
    from telegram import Update
    update = Update.de_json({
        "update_id": 1,
        "message": {"message_id": 5, "date": 1, "chat": {"id": 123, "type": "private", "first_name": "User"},
                    "from": {"id": 123, "is_bot": False, "first_name": "User"},
                    "text": "/start verify_token", "entities": [{"offset": 0, "length": 6, "type": "bot_command"}]},
    }, None)
    assert update.message.text == "/start verify_token"
    assert update.message.entities[0].type == "bot_command"
