from __future__ import annotations

import asyncio

from chatwoot_autoreply_bot.events import ConversationKey
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY


class Recorder:
    def __init__(self, delay: float = 0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.responded: list[ConversationKey] = []
        self.handoffs: list[tuple[ConversationKey, str]] = []
        self.active = 0
        self.max_active = 0

    async def respond(self, key: ConversationKey) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("boom")
            self.responded.append(key)
        finally:
            self.active -= 1

    async def hand_off(self, key: ConversationKey, *, reason: str) -> None:
        self.handoffs.append((key, reason))


def scheduler(store: ValkeyStore, recorder: Recorder, debounce: float = 0.05) -> ReplyScheduler:
    return ReplyScheduler(
        store=store,
        respond=recorder.respond,
        hand_off=recorder.hand_off,
        debounce_seconds=debounce,
    )


async def test_burst_of_messages_yields_one_reply(store: ValkeyStore) -> None:
    recorder = Recorder()
    replies = scheduler(store, recorder)
    for _ in range(4):
        await replies.schedule(KEY)
        await asyncio.sleep(0.01)
    await replies.drain()
    assert recorder.responded == [KEY]


async def test_conversations_are_debounced_independently(store: ValkeyStore) -> None:
    recorder = Recorder()
    replies = scheduler(store, recorder)
    other = ConversationKey(1, 43)
    await replies.schedule(KEY)
    await replies.schedule(other)
    await replies.drain()
    assert sorted(recorder.responded, key=lambda key: key.conversation_id) == [KEY, other]


async def test_replies_in_one_conversation_never_overlap(store: ValkeyStore) -> None:
    recorder = Recorder(delay=0.1)
    replies = scheduler(store, recorder, debounce=0.01)
    await replies.schedule(KEY)
    await asyncio.sleep(0.05)
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.responded == [KEY, KEY]
    assert recorder.max_active == 1


async def test_message_during_a_reply_gets_its_own_turn_after_it(store: ValkeyStore) -> None:
    recorder = Recorder(delay=0.1)
    replies = scheduler(store, recorder, debounce=0.01)
    await replies.schedule(KEY)
    await asyncio.sleep(0.03)
    await replies.schedule(KEY)
    await asyncio.sleep(0.01)
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.responded == [KEY, KEY]


async def test_failure_hands_off(store: ValkeyStore) -> None:
    recorder = Recorder(fail=True)
    replies = scheduler(store, recorder, debounce=0)
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.handoffs == [(KEY, "reply_error")]


async def test_failed_handoff_is_contained(store: ValkeyStore) -> None:
    async def broken_hand_off(key: ConversationKey, *, reason: str) -> None:
        raise RuntimeError("chatwoot down")

    recorder = Recorder(fail=True)
    replies = ReplyScheduler(store=store, respond=recorder.respond, hand_off=broken_hand_off, debounce_seconds=0)
    await replies.schedule(KEY)
    await replies.drain()


async def test_close_cancels_waiting_turns(store: ValkeyStore) -> None:
    recorder = Recorder()
    replies = scheduler(store, recorder, debounce=10)
    await replies.schedule(KEY)
    await replies.aclose()
    assert recorder.responded == []


async def test_lock_lost_after_the_reply_is_not_a_handoff(store: ValkeyStore, valkey) -> None:
    recorder = Recorder()

    async def respond_then_lose_lock(key: ConversationKey) -> None:
        await recorder.respond(key)
        await valkey.delete("cwbot:1:42:lock")

    replies = ReplyScheduler(
        store=store,
        respond=respond_then_lose_lock,
        hand_off=recorder.hand_off,
        debounce_seconds=0,
    )
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.responded == [KEY]
    assert recorder.handoffs == []


async def test_lock_timeout_before_the_reply_hands_off(store: ValkeyStore) -> None:
    class LockFails(ValkeyStore):
        def lock(self, key):
            raise TimeoutError("lock busy")

    recorder = Recorder()
    replies = scheduler(LockFails(store._client, store._limits), recorder, debounce=0)
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.responded == []
    assert recorder.handoffs == [(KEY, "reply_error")]


async def test_turn_superseded_while_waiting_for_the_lock_never_responds(store: ValkeyStore) -> None:
    recorder = Recorder()
    replies = scheduler(store, recorder, debounce=0)
    async with store.lock(KEY):
        await replies.schedule(KEY)
        await asyncio.sleep(0.02)
        await replies.schedule(KEY)
        await asyncio.sleep(0.02)
    await replies.drain()
    assert recorder.responded == [KEY]


async def test_finished_turn_clears_its_outstanding_marker(store: ValkeyStore) -> None:
    replies = scheduler(store, Recorder(), debounce=0)
    await replies.schedule(KEY)
    assert await store.outstanding() == [KEY]
    await replies.drain()
    assert await store.outstanding() == []


async def test_failed_turn_clears_its_marker_after_the_handoff(store: ValkeyStore) -> None:
    recorder = Recorder(fail=True)
    replies = scheduler(store, recorder, debounce=0)
    await replies.schedule(KEY)
    await replies.drain()
    assert recorder.handoffs and await store.outstanding() == []


async def test_cancelled_turn_stays_outstanding_and_resumes(store: ValkeyStore) -> None:
    first = Recorder()
    stopped = scheduler(store, first, debounce=10)
    await stopped.schedule(KEY)
    await stopped.aclose()
    assert await store.outstanding() == [KEY]

    second = Recorder()
    restarted = scheduler(store, second, debounce=0)
    assert await restarted.resume() == 1
    await restarted.drain()
    assert first.responded == []
    assert second.responded == [KEY]
    assert await store.outstanding() == []


async def test_resume_with_nothing_outstanding(store: ValkeyStore) -> None:
    assert await scheduler(store, Recorder()).resume() == 0
