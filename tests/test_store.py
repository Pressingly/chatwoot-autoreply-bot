from __future__ import annotations

from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY, LIMITS


async def test_delivery_is_claimed_once(store: ValkeyStore) -> None:
    assert await store.claim_delivery("d-1")
    assert not await store.claim_delivery("d-1")
    assert await store.claim_delivery("d-2")


async def test_released_delivery_can_be_claimed_again(store: ValkeyStore) -> None:
    await store.claim_delivery("d-1")
    await store.release_delivery("d-1")
    assert await store.claim_delivery("d-1")


async def test_delivery_claim_expires(store: ValkeyStore, valkey) -> None:
    await store.claim_delivery("d-1")
    assert 0 < await valkey.ttl("cwbot:delivery:d-1") <= LIMITS.dedupe_ttl_seconds


async def test_transcript_is_ordered_by_message_id(store: ValkeyStore) -> None:
    await store.append(KEY, TranscriptEntry(20, "assistant", "second"))
    await store.append(KEY, TranscriptEntry(10, "user", "first"))
    assert await store.transcript(KEY) == [
        TranscriptEntry(10, "user", "first"),
        TranscriptEntry(20, "assistant", "second"),
    ]


async def test_same_message_id_overwrites(store: ValkeyStore) -> None:
    await store.append(KEY, TranscriptEntry(10, "assistant", "local copy"))
    await store.append(KEY, TranscriptEntry(10, "assistant", "webhook echo"))
    assert await store.transcript(KEY) == [TranscriptEntry(10, "assistant", "webhook echo")]


async def test_transcript_keeps_the_newest_messages(store: ValkeyStore, valkey) -> None:
    for message_id in range(1, 9):
        await store.append(KEY, TranscriptEntry(message_id, "user", f"m{message_id}"))
    transcript = await store.transcript(KEY)
    assert [entry.message_id for entry in transcript] == [4, 5, 6, 7, 8]
    assert await valkey.hlen("cwbot:1:42:entries") == LIMITS.transcript_max_messages


async def test_transcript_expires(store: ValkeyStore, valkey) -> None:
    await store.append(KEY, TranscriptEntry(1, "user", "hi"))
    for suffix in ("order", "entries"):
        assert 0 < await valkey.ttl(f"cwbot:1:42:{suffix}") <= LIMITS.transcript_ttl_seconds


async def test_transcripts_are_per_conversation(store: ValkeyStore) -> None:
    await store.append(KEY, TranscriptEntry(1, "user", "a"))
    await store.append(ConversationKey(2, 42), TranscriptEntry(2, "user", "b"))
    assert [entry.content for entry in await store.transcript(KEY)] == ["a"]
    assert await store.transcript(ConversationKey(1, 43)) == []


async def test_generation_counts_up(store: ValkeyStore) -> None:
    assert await store.generation(KEY) == 0
    assert await store.bump_generation(KEY) == 1
    assert await store.bump_generation(KEY) == 2
    assert await store.generation(KEY) == 2


async def test_lock_is_exclusive(store: ValkeyStore, valkey) -> None:
    async with store.lock(KEY):
        assert await valkey.exists("cwbot:1:42:lock")
    assert not await valkey.exists("cwbot:1:42:lock")


async def test_ping(store: ValkeyStore) -> None:
    assert await store.ping()
