from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from upstash_redis.asyncio import Redis


@dataclass(slots=True)
class Session:
    token: str
    chat_id: int
    user_id: int
    expires_at: float
    created_at: float
    stage: str = "new"
    x_username: str | None = None
    attempts: int = 0
    last_attempt_at: float = 0.0
    prompt_message_id: int | None = None
    review_message_id: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def from_json(cls, raw: str | dict) -> "Session":
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls(**data)


# Atomic Redis transitions avoid races between Telegram and QStash invocations.
STATE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local s = cjson.decode(raw)
local now = tonumber(ARGV[1])
if now >= s.expires_at then return nil end
local action = ARGV[2]
if action == 'read' then return raw end
if action == 'claim' then
  if s.stage ~= 'checking' and s.stage ~= 'review' then return nil end
  s.stage = 'approving'
elseif action == 'stage' then
  if s.stage ~= ARGV[3] then return nil end
  s.stage = ARGV[4]
elseif action == 'begin_x' then
  if s.stage ~= 'awaiting_x' or tostring(s.user_id) ~= ARGV[3] then return nil end
  if s.attempts >= tonumber(ARGV[4]) then return 'limit' end
  if now - s.last_attempt_at < tonumber(ARGV[5]) then return 'rate_limited' end
  s.stage = 'checking'
  s.x_username = ARGV[6]
  s.attempts = s.attempts + 1
  s.last_attempt_at = now
elseif action == 'message' then
  local value = tonumber(ARGV[3])
  if ARGV[4] == 'prompt' then s.prompt_message_id = value
  else s.review_message_id = value end
else return redis.error_reply('unknown action') end
local encoded = cjson.encode(s)
redis.call('SET', KEYS[1], encoded, 'KEEPTTL')
return encoded
"""

ADD_SCRIPT = """
local old_token = redis.call('GET', KEYS[2])
if old_token then
  local old = redis.call('GET', KEYS[3] .. old_token)
  if old then
    local s = cjson.decode(old)
    if tonumber(ARGV[1]) < s.expires_at then return 'duplicate' end
  end
  redis.call('DEL', KEYS[3] .. old_token)
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
redis.call('SET', KEYS[2], ARGV[4], 'EX', tonumber(ARGV[3]))
return 'ok'
"""

CLAIM_EXPIRED_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local s = cjson.decode(raw)
if tonumber(ARGV[1]) < s.expires_at then return nil end
if redis.call('DEL', KEYS[1]) == 1 then
  if redis.call('GET', KEYS[2]) == ARGV[2] then redis.call('DEL', KEYS[2]) end
  return raw
end
return nil
"""

REMOVE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
if ARGV[1] ~= '' then
  local s = cjson.decode(raw)
  if s.stage ~= ARGV[1] then return nil end
end
if redis.call('DEL', KEYS[1]) == 1 then
  if redis.call('GET', KEYS[2]) == ARGV[2] then redis.call('DEL', KEYS[2]) end
  return raw
end
return nil
"""

RESTORE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 'exists' end
local owner = redis.call('GET', KEYS[2])
if owner and owner ~= ARGV[5] then return 'replaced' end
if tonumber(ARGV[1]) >= tonumber(ARGV[3]) then return 'expired' end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[2], ARGV[5], 'EX', ARGV[4])
return 'ok'
"""


class SessionStore:
    def __init__(self, redis: Redis, namespace: str = "guard") -> None:
        self.redis = redis
        self.namespace = namespace
        self.retention_seconds = 7 * 24 * 60 * 60

    def _key(self, token: str) -> str:
        return f"{self.namespace}:session:{token}"

    def _member_key(self, chat_id: int, user_id: int) -> str:
        return f"{self.namespace}:member:{chat_id}:{user_id}"

    async def add(self, session: Session) -> bool:
        result = await self.redis.eval(
            ADD_SCRIPT,
            keys=[self._key(session.token), self._member_key(session.chat_id, session.user_id), f"{self.namespace}:session:"],
            args=[str(time.time()), session.to_json(), str(self.retention_seconds), session.token],
        )
        return result == "ok"

    async def _state(self, token: str, *args: str) -> str | None:
        result = await self.redis.eval(STATE_SCRIPT, keys=[self._key(token)], args=[str(time.time()), *args])
        if result is None or isinstance(result, dict):
            return None
        return str(result)

    async def get(self, token: str, *, user_id: int | None = None, chat_id: int | None = None) -> Session | None:
        raw = await self._state(token, "read")
        if raw is None:
            return None
        session = Session.from_json(raw)
        if user_id is not None and session.user_id != user_id:
            return None
        if chat_id is not None and session.chat_id != chat_id:
            return None
        return session

    async def for_user(self, chat_id: int, user_id: int) -> Session | None:
        token = await self.redis.get(self._member_key(chat_id, user_id))
        return await self.get(str(token), chat_id=chat_id, user_id=user_id) if token else None

    async def set_stage(self, token: str, stage: str, allowed: str) -> Session | None:
        raw = await self._state(token, "stage", allowed, stage)
        return Session.from_json(raw) if raw else None

    async def set_message(self, token: str, message_id: int, kind: str) -> Session | None:
        raw = await self._state(token, "message", str(message_id), kind)
        return Session.from_json(raw) if raw else None

    async def begin_x(self, token: str, user_id: int, username: str, max_attempts: int, retry_seconds: int) -> tuple[str, Session | None]:
        raw = await self._state(token, "begin_x", str(user_id), str(max_attempts), str(retry_seconds), username)
        if raw in {"limit", "rate_limited"}:
            return raw, None
        return ("ok", Session.from_json(raw)) if raw else ("invalid", None)

    async def claim_completion(self, token: str) -> Session | None:
        raw = await self._state(token, "claim")
        return Session.from_json(raw) if raw else None

    async def expire(self, token: str) -> Session | None:
        raw = await self.redis.get(self._key(token))
        if raw is None:
            return None
        session = Session.from_json(raw)
        result = await self.redis.eval(
            CLAIM_EXPIRED_SCRIPT,
            keys=[self._key(token), self._member_key(session.chat_id, session.user_id)],
            args=[str(time.time()), token],
        )
        return Session.from_json(result) if result else None

    async def remove(self, token: str, *, stage: str = "") -> Session | None:
        current = await self.redis.get(self._key(token))
        if current is None:
            return None
        session = Session.from_json(current)
        result = await self.redis.eval(
            REMOVE_SCRIPT,
            keys=[self._key(token), self._member_key(session.chat_id, session.user_id)],
            args=[stage, token],
        )
        return Session.from_json(result) if result else None

    async def restore(self, session: Session, stage: str) -> Session | None:
        session.stage = stage
        ttl = max(1, int(session.expires_at - time.time()) + self.retention_seconds)
        result = await self.redis.eval(
            RESTORE_SCRIPT,
            keys=[self._key(session.token), self._member_key(session.chat_id, session.user_id)],
            args=[str(time.time()), session.to_json(), str(session.expires_at), str(ttl), session.token],
        )
        return session if result == "ok" else None



    async def set_private(self, user_id: int, token: str) -> None:
        await self.redis.set(f"{self.namespace}:private:{user_id}", token, ex=self.retention_seconds)

    async def private_token(self, user_id: int) -> str | None:
        token = await self.redis.get(f"{self.namespace}:private:{user_id}")
        return str(token) if token else None

    async def clear_private(self, user_id: int, token: str) -> None:
        await self.redis.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
            keys=[f"{self.namespace}:private:{user_id}"], args=[token],
        )
