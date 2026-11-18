"""Valkey state: webhook dedupe, per-conversation transcripts, reply generations and locks.

A transcript is a sorted set of message ids (Chatwoot ids only grow, so the score is
the order) plus a hash from id to entry. Keying by id makes a re-delivered message,
or the webhook echo of a reply the bot already recorded, overwrite rather than repeat.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from redis.asyncio import Redis

from chatwoot_autoreply_bot.digits import parse_digits
from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry

PREFIX = "cwbot"
OUTSTANDING_KEY = f"{PREFIX}:outstanding"

RAISE_TO_MAX = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if tonumber(ARGV[1]) > current then redis.call('SET', KEYS[1], ARGV[1]) end
redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
DELETE_IF_EQUAL = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then return redis.call('HDEL', KEYS[1], ARGV[1]) end
return 0
"""


def _conversation_prefix(key: ConversationKey) -> str:
    return f"{PREFIX}:{key.account_id}:{key.conversation_id}"


@dataclass(frozen=True)
class StoreLimits:
    dedupe_ttl_seconds: int
    transcript_max_messages: int
    transcript_ttl_seconds: int
    lock_ttl_seconds: int


class ValkeyStore:
    def __init__(self, client: Redis, limits: StoreLimits) -> None:
        self._client = client
        self._limits = limits

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def claim_delivery(self, delivery_id: str) -> bool:
        claimed = await self._client.set(
            f"{PREFIX}:delivery:{delivery_id}", "1", nx=True, ex=self._limits.dedupe_ttl_seconds
        )
        return bool(claimed)

    async def release_delivery(self, delivery_id: str) -> None:
        await self._client.delete(f"{PREFIX}:delivery:{delivery_id}")

    async def append(self, key: ConversationKey, entry: TranscriptEntry) -> None:
        order_key, entries_key = self._transcript_keys(key)
        ttl = self._limits.transcript_ttl_seconds
        serialized = json.dumps({"role": entry.role, "content": entry.content})
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zadd(order_key, {str(entry.message_id): entry.message_id})
            pipe.hset(entries_key, str(entry.message_id), serialized)
            pipe.expire(order_key, ttl)
            pipe.expire(entries_key, ttl)
            await pipe.execute()
        await self._trim(order_key, entries_key)

    async def _trim(self, order_key: str, entries_key: str) -> None:
        overflow = await self._client.zcard(order_key) - self._limits.transcript_max_messages
        if overflow <= 0:
            return
        stale = await self._client.zrange(order_key, 0, overflow - 1)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zrem(order_key, *stale)
            pipe.hdel(entries_key, *stale)
            await pipe.execute()

    async def transcript(self, key: ConversationKey) -> list[TranscriptEntry]:
        order_key, entries_key = self._transcript_keys(key)
        ids = await self._client.zrange(order_key, 0, -1)
        if not ids:
            return []
        values = await self._client.hmget(entries_key, ids)
        return [_decode(message_id, value) for message_id, value in zip(ids, values, strict=True) if value]

    async def bump_generation(self, key: ConversationKey) -> int:
        generation_key = f"{_conversation_prefix(key)}:generation"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(generation_key)
            pipe.expire(generation_key, self._limits.transcript_ttl_seconds)
            generation, _ = await pipe.execute()
        return int(generation)

    async def generation(self, key: ConversationKey) -> int:
        value = await self._client.get(f"{_conversation_prefix(key)}:generation")
        return int(value) if value is not None else 0

    async def answered_through(self, key: ConversationKey) -> int:
        value = await self._client.get(f"{_conversation_prefix(key)}:answered")
        return int(value) if value is not None else 0

    async def advance_answered(self, key: ConversationKey, message_id: int) -> None:
        """Mark every customer message up to ``message_id`` answered; never moves backwards."""
        answered_key = f"{_conversation_prefix(key)}:answered"
        await self._client.eval(RAISE_TO_MAX, 1, answered_key, message_id, self._limits.transcript_ttl_seconds)

    async def mark_outstanding(self, key: ConversationKey, generation: int) -> None:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.hset(OUTSTANDING_KEY, _field(key), generation)
            pipe.expire(OUTSTANDING_KEY, self._limits.transcript_ttl_seconds)
            await pipe.execute()

    async def clear_outstanding(self, key: ConversationKey, generation: int) -> None:
        """Clear the marker only if no newer message re-marked it meanwhile."""
        await self._client.eval(DELETE_IF_EQUAL, 1, OUTSTANDING_KEY, _field(key), generation)

    async def outstanding(self) -> list[ConversationKey]:
        fields = await self._client.hkeys(OUTSTANDING_KEY)
        return [key for key in map(_parse_field, fields) if key is not None]

    def lock(self, key: ConversationKey) -> AbstractAsyncContextManager[object]:
        ttl = self._limits.lock_ttl_seconds
        return self._client.lock(f"{_conversation_prefix(key)}:lock", timeout=ttl, blocking_timeout=ttl)

    def _transcript_keys(self, key: ConversationKey) -> tuple[str, str]:
        prefix = _conversation_prefix(key)
        return f"{prefix}:order", f"{prefix}:entries"


def _field(key: ConversationKey) -> str:
    return f"{key.account_id}:{key.conversation_id}"


def _parse_field(raw: bytes | str) -> ConversationKey | None:
    text = raw.decode() if isinstance(raw, bytes) else raw
    account, _, conversation = text.partition(":")
    account_id, conversation_id = parse_digits(account), parse_digits(conversation)
    if account_id is None or conversation_id is None:
        return None
    return ConversationKey(account_id, conversation_id)


def _decode(message_id: bytes | str, value: bytes | str) -> TranscriptEntry:
    data = json.loads(value)
    return TranscriptEntry(int(message_id), data["role"], data["content"])
