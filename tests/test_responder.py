from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from chatwoot_autoreply_bot.events import TranscriptEntry
from chatwoot_autoreply_bot.llm import HANDOFF_SENTINEL, LlmClient
from chatwoot_autoreply_bot.responder import (
    Outcome,
    Responder,
    asks_for_human,
    llm_context,
    unanswered_customer_messages,
)
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY, FakeChatwoot, completion_response

HANDOFF_NOTE = "A teammate will reply shortly."
PHRASES = ("talk to a human",)


class FakeLlm:
    def __init__(self, reply: str | None = "Happy to help!", status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=completion_response(self.reply))

    def client(self) -> LlmClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return LlmClient(AsyncOpenAI(api_key="k", base_url="https://llm.test/v1", http_client=http, max_retries=0), "m")


async def no_sleep(_: float) -> None:
    return None


def responder(chatwoot: FakeChatwoot, store: ValkeyStore, llm: FakeLlm) -> Responder:
    return Responder(
        sleep=no_sleep,
        chatwoot=chatwoot.client(),
        llm=llm.client(),
        store=store,
        prompt_for=lambda account_id: f"prompt for {account_id}",
        handoff_message=HANDOFF_NOTE,
        handoff_phrases=PHRASES,
        llm_budget_seconds=30,
    )


async def seed(store: ValkeyStore, *entries: TranscriptEntry) -> None:
    for entry in entries:
        await store.append(KEY, entry)


def test_unanswered_messages_are_customer_messages_past_the_watermark() -> None:
    transcript = [
        TranscriptEntry(1, "user", "a"),
        TranscriptEntry(2, "user", "b"),
        TranscriptEntry(1001, "assistant", "reply to a"),
        TranscriptEntry(3, "user", "c"),
    ]
    assert [entry.content for entry in unanswered_customer_messages(transcript, 1)] == ["b", "c"]
    assert unanswered_customer_messages(transcript, 3) == []


def test_llm_context_puts_unanswered_messages_last() -> None:
    transcript = [
        TranscriptEntry(1, "user", "a"),
        TranscriptEntry(2, "user", "b"),
        TranscriptEntry(1001, "assistant", "reply to a"),
    ]
    unanswered = unanswered_customer_messages(transcript, 1)
    assert [entry.content for entry in llm_context(transcript, unanswered)] == ["a", "reply to a", "b"]


def test_asks_for_human_is_case_insensitive() -> None:
    assert asks_for_human([TranscriptEntry(1, "user", "Can I TALK TO A HUMAN?")], PHRASES)
    assert not asks_for_human([TranscriptEntry(1, "user", "Are you human?")], PHRASES)


async def test_replies_with_typing_and_records_the_reply(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    llm = FakeLlm()
    await seed(store, TranscriptEntry(1, "user", "Hi"), TranscriptEntry(2, "user", "Open today?"))

    assert await responder(chatwoot, store, llm).respond(KEY) is Outcome.REPLIED

    assert chatwoot.writes() == ["/toggle_typing_status", "/toggle_typing_status", "/messages"]
    assert chatwoot.posted_messages() == ["Happy to help!"]
    assert llm.requests[0]["messages"][0]["content"].startswith("prompt for 1")
    assert [m["content"] for m in llm.requests[0]["messages"][1:]] == ["Hi", "Open today?"]
    assert (await store.transcript(KEY))[-1] == TranscriptEntry(1001, "assistant", "Happy to help!")


@pytest.mark.parametrize("status", ["open", "resolved", "snoozed"])
async def test_never_replies_outside_pending(chatwoot: FakeChatwoot, store: ValkeyStore, status: str) -> None:
    chatwoot.status = status
    llm = FakeLlm()
    await seed(store, TranscriptEntry(1, "user", "Hi"))

    assert await responder(chatwoot, store, llm).respond(KEY) is Outcome.SKIPPED
    assert chatwoot.writes() == []
    assert llm.requests == []


async def test_skips_when_nothing_is_unanswered(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    llm = FakeLlm()
    await seed(store, TranscriptEntry(1, "user", "Hi"), TranscriptEntry(2, "assistant", "Hello"))
    await store.advance_answered(KEY, 1)
    assert await responder(chatwoot, store, llm).respond(KEY) is Outcome.SKIPPED
    assert llm.requests == []


async def test_agent_takeover_during_the_llm_call_drops_the_reply(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.statuses = ["pending", "open"]
    await seed(store, TranscriptEntry(1, "user", "Hi"))

    assert await responder(chatwoot, store, FakeLlm()).respond(KEY) is Outcome.SKIPPED
    assert chatwoot.posted_messages() == []


async def test_customer_asking_for_a_human_hands_off_without_the_llm(
    chatwoot: FakeChatwoot, store: ValkeyStore
) -> None:
    llm = FakeLlm()
    await seed(store, TranscriptEntry(1, "user", "I want to talk to a human"), TranscriptEntry(2, "user", "now"))

    assert await responder(chatwoot, store, llm).respond(KEY) is Outcome.HANDED_OFF
    assert llm.requests == []
    assert chatwoot.posted_messages() == [HANDOFF_NOTE]
    assert chatwoot.writes()[-1] == "/toggle_status"


@pytest.mark.parametrize("reply", [HANDOFF_SENTINEL, f"Let me check. {HANDOFF_SENTINEL}", "  "])
async def test_llm_handoff_signal_hands_off_and_never_posts_the_sentinel(
    chatwoot: FakeChatwoot, store: ValkeyStore, reply: str
) -> None:
    await seed(store, TranscriptEntry(1, "user", "Refund my order"))

    assert await responder(chatwoot, store, FakeLlm(reply)).respond(KEY) is Outcome.HANDED_OFF
    assert chatwoot.posted_messages() == [HANDOFF_NOTE]
    assert all(HANDOFF_SENTINEL not in message for message in chatwoot.posted_messages())
    assert ("POST", "/toggle_status", {"status": "open"}) in chatwoot.calls()


async def test_llm_failure_hands_off_and_stops_typing(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    await seed(store, TranscriptEntry(1, "user", "Hi"))

    assert await responder(chatwoot, store, FakeLlm(status=500)).respond(KEY) is Outcome.HANDED_OFF
    typing = [body for method, path, body in chatwoot.calls() if path == "/toggle_typing_status"]
    assert typing == [{"typing_status": "on"}, {"typing_status": "off"}]
    assert chatwoot.writes()[-1] == "/toggle_status"


async def test_typing_failures_do_not_block_the_reply(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.fail_paths.add("/toggle_typing_status")
    await seed(store, TranscriptEntry(1, "user", "Hi"))
    assert await responder(chatwoot, store, FakeLlm()).respond(KEY) is Outcome.REPLIED


async def test_handoff_still_opens_when_the_note_fails(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.fail_paths.add("/messages")
    assert await responder(chatwoot, store, FakeLlm()).hand_off(KEY, reason="test") is Outcome.HANDED_OFF
    assert chatwoot.writes()[-1] == "/toggle_status"


async def test_handoff_leaves_a_conversation_a_human_already_owns(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.status = "open"
    assert await responder(chatwoot, store, FakeLlm()).hand_off(KEY, reason="test") is Outcome.SKIPPED
    assert chatwoot.writes() == []


async def test_handoff_proceeds_when_the_status_is_unknown(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.fail_paths.add("")
    assert await responder(chatwoot, store, FakeLlm()).hand_off(KEY, reason="test") is Outcome.HANDED_OFF
    assert chatwoot.writes() == ["/messages", "/toggle_status"]


async def test_failing_to_record_the_reply_does_not_undo_it(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    class AppendFails(ValkeyStore):
        async def append(self, key, entry) -> None:
            raise ConnectionError("valkey evicted")

    seeded = AppendFails(store._client, store._limits)
    await store.append(KEY, TranscriptEntry(1, "user", "Hi"))

    assert await responder(chatwoot, seeded, FakeLlm()).respond(KEY) is Outcome.REPLIED
    assert chatwoot.posted_messages() == ["Happy to help!"]
    assert "/toggle_status" not in chatwoot.writes()


async def test_reply_and_handoff_advance_the_watermark(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    await seed(store, TranscriptEntry(1, "user", "Hi"), TranscriptEntry(2, "user", "Open?"))
    await responder(chatwoot, store, FakeLlm()).respond(KEY)
    assert await store.answered_through(KEY) == 2

    await seed(store, TranscriptEntry(3, "user", "talk to a human"))
    assert await responder(chatwoot, store, FakeLlm()).respond(KEY) is Outcome.HANDED_OFF
    assert await store.answered_through(KEY) == 3


async def test_handoff_retries_the_open_without_reposting_the_note(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    attempts: list[int] = []
    original = chatwoot.handler

    def flaky(request):
        if request.url.path.endswith("/toggle_status"):
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(500)
        return original(request)

    chatwoot.handler = flaky
    assert await responder(chatwoot, store, FakeLlm()).hand_off(KEY, reason="test") is Outcome.HANDED_OFF
    assert len(attempts) == 3
    assert chatwoot.posted_messages() == [HANDOFF_NOTE]


async def test_handoff_gives_up_after_bounded_open_attempts(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    chatwoot.fail_paths.add("/toggle_status")
    assert await responder(chatwoot, store, FakeLlm()).hand_off(KEY, reason="test") is Outcome.HANDOFF_FAILED
    assert chatwoot.writes().count("/toggle_status") == 3
    assert chatwoot.posted_messages() == [HANDOFF_NOTE]


async def test_handoff_never_raises(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    bot = responder(chatwoot, store, FakeLlm())

    async def explode(key):
        raise RuntimeError("unexpected")

    bot._post_handoff_note = explode
    assert await bot.hand_off(KEY, reason="test") is Outcome.HANDOFF_FAILED


async def test_failing_watermark_does_not_undo_the_reply(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    class WatermarkFails(ValkeyStore):
        async def advance_answered(self, key, message_id) -> None:
            raise ConnectionError("down")

    broken = WatermarkFails(store._client, store._limits)
    await store.append(KEY, TranscriptEntry(1, "user", "Hi"))
    assert await responder(chatwoot, broken, FakeLlm()).respond(KEY) is Outcome.REPLIED
