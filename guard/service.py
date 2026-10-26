from __future__ import annotations

import html
import logging
import re
import secrets
import time
from enum import Enum

import httpx
from qstash import AsyncQStash, Receiver
from telegram import Bot, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from upstash_redis.asyncio import Redis

from .config import Settings
from .store import Session, SessionStore

LOGGER = logging.getLogger("telegram_guard_bot.cloud")
USERNAME_PATTERN = re.compile(r"@?([A-Za-z0-9_]{1,15})")
FOLLOW_API_URL = "https://api.twitterapi.io/twitter/user/check_follow_relationship"


class Runtime:
    """Reused by warm function instances; credentials are never sent to clients."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = Bot(settings.tg_token)
        self.store = SessionStore(Redis(url=settings.redis_url, token=settings.redis_token))
        self.qstash = AsyncQStash(settings.qstash_token)
        self.receiver = Receiver(
            current_signing_key=settings.qstash_current_signing_key,
            next_signing_key=settings.qstash_next_signing_key,
        )
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.x_request_timeout))

    async def startup(self) -> None:
        await self.bot.initialize()

    async def shutdown(self) -> None:
        await self.http.aclose()
        await self.store.redis.close()
        await self.qstash.http._client.aclose()
        await self.bot.shutdown()


class XServiceUnavailable(Exception):
    pass


class XAccountNotFound(Exception):
    pass


class CompletionResult(Enum):
    APPROVED = "approved"
    EXPIRED = "expired"
    TELEGRAM_ERROR = "telegram_error"


class KickResult(Enum):
    KICKED = "kicked"
    NOT_MEMBER = "not_member"
    ALREADY_UNRESTRICTED = "already_unrestricted"
    ERROR = "error"


def normalize_x_username(text: str) -> str:
    match = USERNAME_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValueError("X 用户名只能包含 1-15 位字母、数字或下划线")
    return match.group(1)


def member_is_present(member: object) -> bool:
    status = getattr(member, "status", None)
    if status in {"member", "administrator", "creator"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


def is_new_member(old: object, new: object) -> bool:
    return not member_is_present(old) and member_is_present(new)


def is_departure(old: object, new: object) -> bool:
    return member_is_present(old) and not member_is_present(new)


def chat_allowed(runtime: Runtime, chat_id: int) -> bool:
    allowed = runtime.settings.allowed_chat_ids
    return not allowed or chat_id in allowed


def mention(user: User) -> str:
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name or str(user.id))}</a>'


def keyboard(settings: Settings, token: str, bot_username: str, private: bool) -> InlineKeyboardMarkup:
    second = (
        InlineKeyboardButton("② 前往私聊提交 X 用户名", url=f"https://t.me/{bot_username}?start=verify_{token}")
        if private
        else InlineKeyboardButton("② 点击开始验证", callback_data=f"v:{token}")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("① 关注我们的 X 账号", url=settings.x_profile_url)],
        [second],
    ])


async def answer(query: object, text: str) -> None:
    try:
        await query.answer(text=text, show_alert=True)  # type: ignore[attr-defined]
    except TelegramError:
        LOGGER.info("回调提示发送失败", exc_info=True)


async def delete_message(runtime: Runtime, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await runtime.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        if "message to delete not found" not in str(exc).lower():
            LOGGER.warning("删除消息失败: %s", exc)
    except TelegramError:
        LOGGER.warning("删除消息异常", exc_info=True)


async def delete_session_messages(runtime: Runtime, session: Session) -> None:
    for message_id in {session.prompt_message_id, session.review_message_id}:
        await delete_message(runtime, session.chat_id, message_id)


async def private_message(runtime: Runtime, user_id: int, text: str) -> None:
    try:
        await runtime.bot.send_message(chat_id=user_id, text=text)
    except TelegramError:
        LOGGER.info("私聊提示发送失败 user_id=%s", user_id, exc_info=True)


async def admin_alert(runtime: Runtime, chat_id: int, text: str) -> None:
    LOGGER.warning("需要管理员处理 chat_id=%s: %s", chat_id, text)
    try:
        await runtime.bot.send_message(chat_id=chat_id, text=f"⚠️ 管理员提醒：{text}")
    except TelegramError:
        LOGGER.error("管理员提醒发送失败", exc_info=True)


async def check_profile(runtime: Runtime, user: User) -> str | None:
    if not user.first_name or not user.first_name.strip():
        return "❌ 您未设置 Telegram 名字，请设置后重试。"
    try:
        photos = await runtime.bot.get_user_profile_photos(user_id=user.id, limit=1)
    except TelegramError:
        LOGGER.warning("读取头像失败 user_id=%s", user.id, exc_info=True)
        return "⚠️ 暂时无法读取头像，请稍后重试。"
    if photos.total_count <= 0:
        return "❌ 您未设置头像，请设置后重试。"
    return None


def response_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = payload.get("message", "") if isinstance(payload, dict) else ""
    except ValueError:
        message = ""
    return re.sub(r"\s+", " ", str(message)).strip()[:200]


def missing_account(message: str) -> bool:
    text = message.lower()
    return any(value in text for value in ("user not found", "not found", "does not exist", "invalid username"))


async def check_x_follow(runtime: Runtime, source_username: str) -> bool:
    try:
        response = await runtime.http.get(
            FOLLOW_API_URL,
            params={"source_user_name": source_username, "target_user_name": runtime.settings.target_x_username},
            headers={"X-API-Key": runtime.settings.twitterapi_key, "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise XServiceUnavailable(f"网络请求失败: {type(exc).__name__}") from exc
    message = response_message(response)
    if response.status_code != 200:
        if response.status_code in {400, 404} and missing_account(message):
            raise XAccountNotFound(message or "X 账号不存在")
        raise XServiceUnavailable(f"HTTP {response.status_code}: {message}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise XServiceUnavailable("接口未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise XServiceUnavailable("接口响应结构异常")
    if payload.get("status") == "error":
        error = str(payload.get("message", ""))[:200]
        if missing_account(error):
            raise XAccountNotFound(error)
        raise XServiceUnavailable(f"接口业务错误: {error}")
    if payload.get("status") != "success":
        raise XServiceUnavailable("接口响应缺少 status=success")
    data = payload.get("data")
    following = data.get("following") if isinstance(data, dict) else None
    if type(following) is not bool:
        raise XServiceUnavailable("接口响应缺少布尔字段 data.following")
    return following


async def ensure_review(runtime: Runtime, session: Session, reason: str) -> None:
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 人工通过", callback_data=f"ma:{session.token}"),
        InlineKeyboardButton("拒绝并踢出", callback_data=f"mr:{session.token}"),
    ]])
    text = (
        "⚠️ 入群人工审核\n"
        f"Telegram 用户 ID：<code>{session.user_id}</code>\n"
        f"申报的 X 用户名：@{html.escape(session.x_username or '未提供')}\n"
        f"原因：{html.escape(reason)}\n"
        "仅配置的 ADMIN_IDS 可操作；五分钟计时不会暂停。"
    )
    try:
        if session.review_message_id is not None:
            try:
                await runtime.bot.edit_message_text(
                    chat_id=session.chat_id, message_id=session.review_message_id,
                    text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
                )
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
        sent = await runtime.bot.send_message(
            chat_id=session.chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
        )
        if await runtime.store.set_message(session.token, sent.message_id, "review") is None:
            await delete_message(runtime, session.chat_id, sent.message_id)
    except TelegramError:
        LOGGER.error("审核消息发送失败 chat_id=%s", session.chat_id, exc_info=True)
        await admin_alert(runtime, session.chat_id, f"用户 {session.user_id} 待人工审核，但审核卡片发送失败。")


async def approve(runtime: Runtime, token: str) -> CompletionResult:
    session = await runtime.store.claim_completion(token)
    if session is None:
        return CompletionResult.EXPIRED
    try:
        await runtime.bot.restrict_chat_member(
            chat_id=session.chat_id, user_id=session.user_id,
            permissions=ChatPermissions.all_permissions(), use_independent_chat_permissions=True,
        )
    except TelegramError:
        LOGGER.error("解除禁言失败 user_id=%s", session.user_id, exc_info=True)
        if await runtime.store.set_stage(token, "review", "approving") is not None:
            await ensure_review(runtime, session, "机器人解除禁言失败，请检查管理员权限后重试")
            return CompletionResult.TELEGRAM_ERROR
        return CompletionResult.EXPIRED
    removed = await runtime.store.remove(token, stage="approving")
    await delete_session_messages(runtime, session)
    if removed is None:
        return CompletionResult.EXPIRED
    await runtime.store.clear_private(session.user_id, token)
    LOGGER.info("验证通过 chat_id=%s user_id=%s", session.chat_id, session.user_id)
    return CompletionResult.APPROVED


async def kick(runtime: Runtime, session: Session, only_if_restricted: bool) -> KickResult:
    try:
        member = await runtime.bot.get_chat_member(session.chat_id, session.user_id)
    except TelegramError:
        LOGGER.error("读取成员状态失败 user_id=%s", session.user_id, exc_info=True)
        return KickResult.ERROR
    if member.status in {"left", "kicked"}:
        return KickResult.NOT_MEMBER
    if only_if_restricted and member.status != "restricted":
        return KickResult.ALREADY_UNRESTRICTED
    try:
        await runtime.bot.ban_chat_member(chat_id=session.chat_id, user_id=session.user_id, revoke_messages=False)
    except TelegramError:
        LOGGER.error("ban 操作失败 user_id=%s", session.user_id, exc_info=True)
        return KickResult.ERROR
    try:
        await runtime.bot.unban_chat_member(
            chat_id=session.chat_id, user_id=session.user_id, only_if_banned=True,
        )
    except TelegramError:
        LOGGER.critical("ban 成功但 unban 失败，用户仍被封禁 user_id=%s", session.user_id, exc_info=True)
        return KickResult.ERROR
    return KickResult.KICKED


async def handle_timeout(token: str, runtime: Runtime) -> str:
    session = await runtime.store.expire(token)
    if session is None:
        return "not_expired"
    result = await kick(runtime, session, only_if_restricted=True)
    await runtime.store.clear_private(session.user_id, token)
    if result == KickResult.ERROR:
        await admin_alert(runtime, session.chat_id, f"用户 {session.user_id} 验证超时，但自动踢出失败，请立即检查。")
        return "error"
    await delete_session_messages(runtime, session)
    if result == KickResult.KICKED:
        await private_message(runtime, session.user_id, "⏰ 验证超时，你已被移出群聊；可通过邀请链接重新加入。")
    return result.value

async def on_chat_member(update: Update, runtime: Runtime) -> None:
    change = update.chat_member
    if change is None:
        return
    settings = runtime.settings
    chat_id = change.chat.id
    user = change.new_chat_member.user
    LOGGER.info(
        "收到成员状态变化 chat_id=%s user_id=%s old_status=%s new_status=%s",
        chat_id, user.id, change.old_chat_member.status, change.new_chat_member.status,
    )
    if not chat_allowed(runtime, chat_id):
        LOGGER.warning("入群事件被群白名单跳过 chat_id=%s；请核对 ALLOWED_CHAT_IDS", chat_id)
        return
    old, new = change.old_chat_member, change.new_chat_member
    if is_departure(old, new):
        session = await runtime.store.for_user(chat_id, user.id)
        if session is not None and await runtime.store.remove(session.token) is not None:
            await runtime.store.clear_private(user.id, session.token)
            await delete_session_messages(runtime, session)
        return
    if not is_new_member(old, new) or user.is_bot or new.status in {"administrator", "creator"}:
        LOGGER.info("忽略非普通新成员状态变化 chat_id=%s user_id=%s", chat_id, user.id)
        return
    if await runtime.store.for_user(chat_id, user.id) is not None:
        return
    try:
        await runtime.bot.restrict_chat_member(
            chat_id=chat_id, user_id=user.id,
            permissions=ChatPermissions(
                can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                can_add_web_page_previews=False, can_change_info=False,
                can_invite_users=False, can_pin_messages=False, can_manage_topics=False,
            ),
            use_independent_chat_permissions=True,
        )
    except TelegramError:
        LOGGER.error("新成员禁言失败 chat_id=%s user_id=%s", chat_id, user.id, exc_info=True)
        await admin_alert(runtime, chat_id, f"无法禁言新成员 {user.id}，请检查机器人管理员权限。")
        return

    now = time.time()
    session = Session(
        token=secrets.token_urlsafe(16), chat_id=chat_id, user_id=user.id,
        created_at=now, expires_at=now + settings.timeout_seconds,
    )
    if not await runtime.store.add(session):
        LOGGER.info("重复入群事件 chat_id=%s user_id=%s", chat_id, user.id)
        return
    try:
        await runtime.qstash.message.publish_json(
            url=settings.timeout_webhook_url, body={"token": session.token},
            delay=settings.timeout_seconds, retries=3,
            deduplication_id=f"guard-timeout-{session.token}",
        )
    except Exception:
        LOGGER.exception("QStash 超时任务创建失败 user_id=%s", user.id)
        await runtime.store.remove(session.token)
        await admin_alert(runtime, chat_id, f"用户 {user.id} 已禁言，但无法创建超时任务；请人工处理。")
        return
    text = (
        f"🔐 入群验证\n{mention(user)}，你已被临时禁言，请在 "
        f"{settings.timeout_seconds // 60} 分钟内完成：\n"
        f"1. 设置 Telegram 名字和头像；\n2. 关注 X @{html.escape(settings.target_x_username)}；\n"
        "3. 点击开始验证，再前往私聊提交 X 用户名。\n超时未完成将被移出群聊。"
    )
    try:
        prompt = await runtime.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard(settings, session.token, runtime.bot.username, private=False),
        )
        if await runtime.store.set_message(session.token, prompt.message_id, "prompt") is None:
            await delete_message(runtime, chat_id, prompt.message_id)
    except TelegramError:
        LOGGER.error("验证引导发送失败 user_id=%s", user.id, exc_info=True)
        # 保留会话，QStash 到期仍能处理，避免永久禁言。
        await admin_alert(runtime, chat_id, f"用户 {user.id} 已禁言，但验证引导发送失败。")


async def on_verify_button(update: Update, runtime: Runtime) -> None:
    query = update.callback_query
    if query is None or query.message is None or not query.data:
        return
    if not chat_allowed(runtime, query.message.chat.id):
        await answer(query, "该群未启用验证。")
        return
    token = query.data[2:]
    session = await runtime.store.get(token, user_id=query.from_user.id, chat_id=query.message.chat.id)
    if session is None:
        await answer(query, "❌ 验证不存在、不属于你，或已经过期。")
        return
    error = await check_profile(runtime, query.from_user)
    if error:
        await answer(query, error)
        return
    if session.stage in {"new", "ready"}:
        await runtime.store.set_stage(token, "ready", session.stage)
    await answer(query, "✅ 名字和头像检查通过，请点击新的私聊按钮。")
    try:
        await query.edit_message_reply_markup(
            reply_markup=keyboard(runtime.settings, token, runtime.bot.username, private=True)
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            LOGGER.warning("更新按钮失败: %s", exc)
    except TelegramError:
        LOGGER.warning("更新按钮异常", exc_info=True)


async def on_start(update: Update, runtime: Runtime, args: list[str]) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    token = args[0][7:] if args and args[0].startswith("verify_") else ""
    session = await runtime.store.get(token, user_id=user.id) if token else None
    if session is None:
        await message.reply_text("请从对应群的验证按钮重新进入私聊。")
        return
    if session.stage == "review":
        await message.reply_text("你的验证已转人工审核；五分钟计时仍继续。")
        return
    if session.stage in {"checking", "approving"}:
        await message.reply_text("正在处理你的验证，请稍候。")
        return
    if session.stage not in {"new", "ready", "awaiting_x"}:
        await message.reply_text("当前状态不可提交，请重新点击群内按钮。")
        return
    error = await check_profile(runtime, user)
    if error:
        await message.reply_text(error)
        return
    if await runtime.store.set_stage(session.token, "awaiting_x", session.stage) is None:
        await message.reply_text("验证已更新或过期，请重新从群内按钮进入。")
        return
    await runtime.store.set_private(user.id, session.token)
    await message.reply_text(
        f"请直接发送你的 X 用户名，例如 @UserA。\n将检查是否关注 @{runtime.settings.target_x_username}。\n"
        "无需也不要发送 X 密码、Cookie、验证码或私钥。"
    )

async def on_x_username(update: Update, runtime: Runtime) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or not message.text:
        return
    token = await runtime.store.private_token(user.id)
    session = await runtime.store.get(token, user_id=user.id) if token else None
    if session is None:
        await message.reply_text("请重新点击群内验证按钮，以确定要验证的群。")
        return
    if session.stage == "review":
        await message.reply_text("已转人工审核，请等待管理员处理。")
        return
    if session.stage != "awaiting_x":
        await message.reply_text("请先点击群内按钮并完成私聊 /start。")
        return
    error = await check_profile(runtime, user)
    if error:
        await message.reply_text(error)
        return
    try:
        username = normalize_x_username(message.text)
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}，例如：@UserA")
        return
    status, session = await runtime.store.begin_x(
        session.token, user.id, username, runtime.settings.max_x_attempts, runtime.settings.x_retry_seconds,
    )
    if status == "rate_limited":
        await message.reply_text(f"查询过于频繁，请 {runtime.settings.x_retry_seconds} 秒后再试。")
        return
    if status == "limit":
        await message.reply_text("已达到本次验证的查询次数上限，请联系管理员。")
        return
    if status != "ok" or session is None:
        await message.reply_text("当前无法查询，请重新从群内按钮进入。")
        return
    await message.reply_text("正在查询 X 关注状态，请稍候……")
    try:
        following = await check_x_follow(runtime, username)
    except XAccountNotFound:
        restored = await runtime.store.set_stage(session.token, "awaiting_x", "checking")
        await message.reply_text("❌ 未找到该 X 账号，请检查后重新发送。" if restored else "⏰ 验证已过期。")
        return
    except XServiceUnavailable as exc:
        LOGGER.warning("X 查询不可用 chat_id=%s user_id=%s: %s", session.chat_id, user.id, exc)
        if runtime.settings.fail_open:
            LOGGER.warning("X_API_FAIL_OPEN=True：跳过 X 检查，未证明已关注 user_id=%s", user.id)
            result = await approve(runtime, session.token)
            await message.reply_text({
                CompletionResult.APPROVED: "✅ 名字和头像已通过；X 接口故障，已按配置降级放行。",
                CompletionResult.EXPIRED: "⏰ 验证已过期。",
                CompletionResult.TELEGRAM_ERROR: "解除禁言失败，已转管理员处理。",
            }[result])
            return
        if await runtime.store.set_stage(session.token, "review", "checking") is None:
            await message.reply_text("⏰ 验证已过期。")
            return
        await ensure_review(runtime, session, "X 查询接口故障或额度不足，未确认关注状态")
        await message.reply_text("⚠️ 自动查询暂不可用，已转管理员审核；五分钟计时仍继续。")
        return
    if not following:
        if await runtime.store.set_stage(session.token, "awaiting_x", "checking") is None:
            await message.reply_text("⏰ 验证已过期。")
            return
        await message.reply_text(
            f"❌ 未检测到 @{username} 关注 @{runtime.settings.target_x_username}。\n"
            "请确认已关注；刚关注时第三方数据可能有延迟，稍后重新发送用户名。"
        )
        return
    result = await approve(runtime, session.token)
    await message.reply_text({
        CompletionResult.APPROVED: "✅ 三项检查通过，已解除群内禁言。",
        CompletionResult.EXPIRED: "⏰ 验证已过期，无法解除禁言。",
        CompletionResult.TELEGRAM_ERROR: "关注检查通过，但解除禁言失败，已转管理员处理。",
    }[result])


async def on_manual_review(update: Update, runtime: Runtime) -> None:
    query = update.callback_query
    if query is None or query.message is None or not query.data:
        return
    if not chat_allowed(runtime, query.message.chat.id):
        await answer(query, "该群未启用验证。")
        return
    if query.from_user.id not in runtime.settings.admin_ids:
        await answer(query, "❌ 只有配置的管理员可以执行此操作。")
        return
    approved = query.data.startswith("ma:")
    token = query.data[3:]
    session = await runtime.store.get(token, chat_id=query.message.chat.id)
    if session is None or session.stage != "review":
        await answer(query, "该审核已处理或已过期。")
        return
    if approved:
        result = await approve(runtime, token)
        if result == CompletionResult.APPROVED:
            await answer(query, "✅ 已人工通过并解除禁言。")
            await private_message(runtime, session.user_id, "✅ 管理员已通过你的入群验证。")
        elif result == CompletionResult.TELEGRAM_ERROR:
            await answer(query, "解除禁言失败，请检查权限后重试。")
        else:
            await answer(query, "该验证已过期。")
        return
    claimed = await runtime.store.remove(token, stage="review")
    if claimed is None:
        await answer(query, "该审核已处理或已过期。")
        return
    result = await kick(runtime, session, only_if_restricted=False)
    if result in {KickResult.KICKED, KickResult.NOT_MEMBER}:
        await runtime.store.clear_private(session.user_id, token)
        await delete_session_messages(runtime, session)
        await answer(query, "已拒绝验证并处理该成员。")
        await private_message(runtime, session.user_id, "管理员未通过你的入群验证。")
    else:
        await runtime.store.restore(session, "review")
        await admin_alert(runtime, session.chat_id, f"管理员拒绝用户 {session.user_id} 后，自动处理失败，请人工检查。")
        await answer(query, "操作失败，请查看管理员提醒。")


async def handle_update(update: Update, runtime: Runtime) -> None:
    """Use PTB Update/Bot objects without polling or in-memory CallbackContext."""
    if update.chat_member is not None:
        await on_chat_member(update, runtime)
        return
    query = update.callback_query
    if query is not None and query.data:
        if query.data.startswith("v:"):
            await on_verify_button(update, runtime)
        elif query.data.startswith(("ma:", "mr:")):
            await on_manual_review(update, runtime)
        return
    message = update.effective_message
    if message is None or message.chat.type != "private" or not message.text:
        return
    entities = message.entities or []
    is_command = bool(entities and entities[0].type == "bot_command" and entities[0].offset == 0)
    command, _, payload = message.text.partition(" ")
    if is_command and command.split("@")[0] == "/start":
        await on_start(update, runtime, [payload] if payload else [])
    elif not is_command:
        await on_x_username(update, runtime)
