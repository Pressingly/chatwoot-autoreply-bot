"""Real Responder, store and scheduler together: the races the unit tests cannot see."""

from __future__ import annotations

import asyncio

from chatwoot_autoreply_bot.events import TranscriptEntry
from chatwoot_autoreply_bot.llm import HANDOFF_SENTINEL
from chatwoot_autoreply_bot.responder import Responder
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY, FakeChatwoot, static_llms
from tests.test_responder import FakeLlm, no_sleep


def wire(store: ValkeyStore, chatwoot: FakeChatwoot, llm: FakeLlm) -> tuple[Responder, ReplyScheduler]:
    responder = Responder(
        chatwoot=chatwoot.client(),
        llms=static_llms(llm.client()),
        store=store,
        prompt_for=lambda _: "p",
        handoff_message="H",
        handoff_phrases=(),
        llm_budget_seconds=30,
        sleep=no_sleep,
    )
    scheduler = ReplyScheduler(
        store=store,
        respond=responder.respond,
        hand_off=responder.hand_off,
        debounce_seconds=0,
    )
    return responder, scheduler


async def test_message_arriving_during_the_llm_call_gets_its_own_reply(
    store: ValkeyStore, chatwoot: FakeChatwoot
) -> None:
    llm = FakeLlm()
    responder, scheduler = wire(store, chatwoot, llm)
    complete = responder._llms._fallback.complete
    calls = 0

    async def customer_writes_mid_call(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            await store.append(KEY, TranscriptEntry(2, "user", "B arrives mid-LLM"))
        return await complete(messages)

    responder._llms._fallback.complete = customer_writes_mid_call
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


def delayed(chatwoot: FakeChatwoot, path_suffix: str, seconds: float, times: int = 1) -> None:
    original = chatwoot.handler
    remaining = {"n": times}

    async def handler(request):
        if request.url.path.endswith(path_suffix) and remaining["n"] > 0:
            remaining["n"] -= 1
            await asyncio.sleep(seconds)
        return original(request)

    chatwoot.handler = handler


async def test_slow_open_during_handoff_never_reposts_the_note(store: ValkeyStore, chatwoot: FakeChatwoot) -> None:
    delayed(chatwoot, "/toggle_status", 0.5)
    responder, scheduler = wire(store, chatwoot, FakeLlm(status=500))
    responder._llm_budget_seconds = 0.2
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await scheduler.drain()
    assert chatwoot.posted_messages() == ["H"]
    assert chatwoot.writes()[-1] == "/toggle_status"


async def test_llm_over_budget_is_one_handoff(store: ValkeyStore, chatwoot: FakeChatwoot) -> None:
    responder, scheduler = wire(store, chatwoot, FakeLlm())
    responder._llm_budget_seconds = 0.05

    async def hang(messages):
        await asyncio.sleep(60)
        return "Too late"

    responder._llms._fallback.complete = hang
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await asyncio.wait_for(scheduler.drain(), 1)
    assert chatwoot.posted_messages() == ["H"]
    assert await store.answered_through(KEY) == 1
    typing = [body for _, path, body in chatwoot.calls() if path == "/toggle_typing_status"]
    assert typing == [{"typing_status": "on"}, {"typing_status": "off"}]


async def test_slow_post_is_not_cut_by_the_llm_budget(store: ValkeyStore, chatwoot: FakeChatwoot) -> None:
    delayed(chatwoot, "/messages", 0.3)
    responder, scheduler = wire(store, chatwoot, FakeLlm("Answer"))
    responder._llm_budget_seconds = 0.05
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await scheduler.drain()
    assert chatwoot.posted_messages() == ["Answer"]
    assert "/toggle_status" not in chatwoot.writes()


async def test_kill_right_after_the_post_does_not_reply_again_on_resume(
    store: ValkeyStore, chatwoot: FakeChatwoot
) -> None:
    responder, scheduler = wire(store, chatwoot, FakeLlm("Answer"))
    post = responder._chatwoot.post_message

    async def post_then_die(key, content):
        await post(key, content)
        raise asyncio.CancelledError

    responder._chatwoot.post_message = post_then_die
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await scheduler.drain()
    assert await store.outstanding() == [KEY]

    _, restarted = wire(store, chatwoot, FakeLlm("Answer"))
    assert await restarted.resume() == 1
    await restarted.drain()
    assert chatwoot.posted_messages() == ["Answer"]


async def test_kill_during_the_handoff_open_does_not_hand_off_again_on_resume(
    store: ValkeyStore, chatwoot: FakeChatwoot
) -> None:
    responder, scheduler = wire(store, chatwoot, FakeLlm(HANDOFF_SENTINEL))

    async def die(key):
        raise asyncio.CancelledError

    responder._chatwoot.open_conversation = die
    await store.append(KEY, TranscriptEntry(1, "user", "A"))
    await scheduler.schedule(KEY)
    await scheduler.drain()
    assert chatwoot.posted_messages() == ["H"]
    assert await store.outstanding() == [KEY]

    _, restarted = wire(store, chatwoot, FakeLlm(HANDOFF_SENTINEL))
    assert await restarted.resume() == 1
    await restarted.drain()
    assert chatwoot.posted_messages() == ["H"]
