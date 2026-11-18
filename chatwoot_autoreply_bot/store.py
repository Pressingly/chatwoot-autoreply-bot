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

from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry

PREFIX = "cwbot"


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

    def lock(self, key: ConversationKey) -> AbstractAsyncContextManager[object]:
        ttl = self._limits.lock_ttl_seconds
        return self._client.lock(f"{_conversation_prefix(key)}:lock", timeout=ttl, blocking_timeout=ttl)

    def _transcript_keys(self, key: ConversationKey) -> tuple[str, str]:
        prefix = _conversation_prefix(key)
        return f"{prefix}:order", f"{prefix}:entries"


def _decode(message_id: bytes | str, value: bytes | str) -> TranscriptEntry:
    data = json.loads(value)
    return TranscriptEntry(int(message_id), data["role"], data["content"])
