"""Real Responder, store and scheduler together: the races the unit tests cannot see."""

from __future__ import annotations

import asyncio

from chatwoot_autoreply_bot.events import TranscriptEntry
from chatwoot_autoreply_bot.responder import Responder
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY, FakeChatwoot
from tests.test_responder import FakeLlm, no_sleep


def wire(store: ValkeyStore, chatwoot: FakeChatwoot, llm: FakeLlm) -> tuple[Responder, ReplyScheduler]:
    responder = Responder(
        chatwoot=chatwoot.client(),
        llm=llm.client(),
        store=store,
        prompt_for=lambda _: "p",
        handoff_message="H",
        handoff_phrases=(),
        sleep=no_sleep,
    )
    scheduler = ReplyScheduler(
        store=store,
        respond=responder.respond,
        hand_off=responder.hand_off,
        debounce_seconds=0,
        turn_timeout_seconds=30,
    )
    return responder, scheduler


async def test_message_arriving_during_the_llm_call_gets_its_own_reply(
    store: ValkeyStore, chatwoot: FakeChatwoot
) -> None:
    llm = FakeLlm()
    responder, scheduler = wire(store, chatwoot, llm)
    complete = responder._llm.complete
    calls = 0

    async def customer_writes_mid_call(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            await store.append(KEY, TranscriptEntry(2, "user", "B arrives mid-LLM"))
        return await complete(messages)

    responder._llm.complete = customer_writes_mid_call
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await asyncio.sleep(0.05)
    await scheduler.schedule(KEY)
    await scheduler.drain()

    assert len(llm.requests) == 2
    assert llm.requests[1]["messages"][-1]["content"] == "B arrives mid-LLM"
    assert len(chatwoot.posted_messages()) == 2


async def test_open_failure_posts_the_handoff_note_once(store: ValkeyStore, chatwoot: FakeChatwoot) -> None:
    chatwoot.fail_paths.add("/toggle_status")
    _, scheduler = wire(store, chatwoot, FakeLlm(status=500))
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await scheduler.drain()
    assert chatwoot.posted_messages().count("H") == 1


async def test_human_reply_then_new_message_is_not_handed_off_again(store: ValkeyStore, chatwoot: FakeChatwoot) -> None:
    responder, _ = wire(store, chatwoot, FakeLlm("Sure."))
    await store.append(KEY, TranscriptEntry(1, "user", "talk to a person please"))
    await store.append(KEY, TranscriptEntry(2, "assistant", "Agent: I've sorted it."))
    await store.advance_answered(KEY, 2)
    await store.append(KEY, TranscriptEntry(3, "user", "Thanks, one more question"))
    responder._handoff_phrases = ("talk to a person",)
    assert (await responder.respond(KEY)).value == "replied"
