"""The reply path with per-inbox keys: which LLM answers, and the handoff notes when none can."""

from __future__ import annotations

import json

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.events import TranscriptEntry
from chatwoot_autoreply_bot.keystore import InboxKeyStore, KeyCipher
from chatwoot_autoreply_bot.llm import LlmClient, LlmCredentials
from chatwoot_autoreply_bot.llm_router import FALLBACK_REJECTED_NOTE, LlmRouter
from chatwoot_autoreply_bot.responder import Outcome, Responder
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import FERNET_KEY, INBOX_ID, KEY, FakeChatwoot
from tests.test_responder import HANDOFF_NOTE, PHRASES, FakeLlm, no_sleep

CONNECT_URL = "https://chatwoot-autoreply.example.com/connect"
PASTED = LlmCredentials("https://api.openai.com/v1", "inbox-model", "sk-inbox-0000000000")


@pytest.fixture
def keystore(valkey: FakeAsyncRedis) -> InboxKeyStore:
    return InboxKeyStore(valkey, KeyCipher(FERNET_KEY))


class Clients:
    """Hands out a FakeLlm-backed client for each pasted key and remembers which one."""

    def __init__(self, llm: FakeLlm) -> None:
        self.llm = llm
        self.built: list[LlmCredentials] = []

    def __call__(self, credentials: LlmCredentials) -> LlmClient:
        self.built.append(credentials)
        return self.llm.client()


def responder(
    chatwoot: FakeChatwoot,
    store: ValkeyStore,
    *,
    keystore: InboxKeyStore | None,
    pasted: FakeLlm | None = None,
    fallback: FakeLlm | None = None,
) -> tuple[Responder, Clients]:
    clients = Clients(pasted or FakeLlm())
    llms = LlmRouter(
        client_factory=clients,
        keystore=keystore,
        fallback=fallback.client() if fallback else None,
        connect_url=CONNECT_URL,
    )
    bot = Responder(
        sleep=no_sleep,
        chatwoot=chatwoot.client(),
        llms=llms,
        store=store,
        prompt_for=lambda _: "prompt",
        handoff_message=HANDOFF_NOTE,
        handoff_phrases=PHRASES,
        llm_budget_seconds=30,
    )
    return bot, clients


async def customer_says_hi(store: ValkeyStore) -> None:
    await store.append(KEY, TranscriptEntry(1, "user", "Hi"))


async def test_pasted_key_for_the_conversation_inbox_answers(
    chatwoot: FakeChatwoot, store: ValkeyStore, keystore: InboxKeyStore
) -> None:
    await keystore.save(INBOX_ID, PASTED, set_by="1234", set_at=1)
    await customer_says_hi(store)
    pasted, fallback = FakeLlm("From the inbox key"), FakeLlm("From the env key")
    bot, clients = responder(chatwoot, store, keystore=keystore, pasted=pasted, fallback=fallback)

    assert await bot.respond(KEY) is Outcome.REPLIED
    assert clients.built == [PASTED]
    assert chatwoot.posted_messages() == ["From the inbox key"]
    assert pasted.requests[0]["model"] == "m"
    assert fallback.requests == []


async def test_other_inbox_falls_back_to_the_env_key(
    chatwoot: FakeChatwoot, store: ValkeyStore, keystore: InboxKeyStore
) -> None:
    await keystore.save(INBOX_ID + 1, PASTED, set_by="1234", set_at=1)
    await customer_says_hi(store)
    bot, clients = responder(chatwoot, store, keystore=keystore, fallback=FakeLlm("From the env key"))

    assert await bot.respond(KEY) is Outcome.REPLIED
    assert clients.built == []
    assert chatwoot.posted_messages() == ["From the env key"]


async def test_env_fallback_alone_still_works(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    await customer_says_hi(store)
    bot, _ = responder(chatwoot, store, keystore=None, fallback=FakeLlm("Env reply"))
    assert await bot.respond(KEY) is Outcome.REPLIED
    assert chatwoot.posted_messages() == ["Env reply"]


async def test_conversation_without_an_inbox_id_uses_the_fallback(
    chatwoot: FakeChatwoot, store: ValkeyStore, keystore: InboxKeyStore
) -> None:
    chatwoot.inbox_id = None
    await keystore.save(INBOX_ID, PASTED, set_by="1234", set_at=1)
    await customer_says_hi(store)
    bot, clients = responder(chatwoot, store, keystore=keystore, fallback=FakeLlm("Env reply"))
    assert await bot.respond(KEY) is Outcome.REPLIED
    assert clients.built == []


async def test_no_key_anywhere_hands_off_with_a_private_note(
    chatwoot: FakeChatwoot, store: ValkeyStore, keystore: InboxKeyStore
) -> None:
    await customer_says_hi(store)
    bot, _ = responder(chatwoot, store, keystore=keystore)

    assert await bot.respond(KEY) is Outcome.HANDED_OFF
    assert chatwoot.posted_messages() == [HANDOFF_NOTE]
    assert chatwoot.private_notes() == [
        f"Auto-reply has no LLM key for this inbox. An allowed admin can connect one at {CONNECT_URL}."
    ]
    assert chatwoot.writes() == ["/messages", "/messages", "/toggle_status"]
    assert await store.answered_through(KEY) == 1


@pytest.mark.parametrize(
    ("status", "error"), [(401, "AuthenticationError"), (403, "PermissionDeniedError"), (404, "NotFoundError")]
)
async def test_rejected_pasted_key_hands_off_once_without_retrying(
    chatwoot: FakeChatwoot, store: ValkeyStore, keystore: InboxKeyStore, status: int, error: str
) -> None:
    await keystore.save(INBOX_ID, PASTED, set_by="1234", set_at=1)
    await customer_says_hi(store)
    pasted = FakeLlm(status=status)
    bot, _ = responder(chatwoot, store, keystore=keystore, pasted=pasted, fallback=FakeLlm())

    assert await bot.respond(KEY) is Outcome.HANDED_OFF
    assert len(pasted.requests) == 1
    assert chatwoot.private_notes() == [
        f"LLM key rejected for this inbox. An allowed admin can reconnect it at {CONNECT_URL}. Provider error: {error}."
    ]
    assert chatwoot.writes()[-1] == "/toggle_status"


async def test_rejected_env_key_names_the_operator(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    await customer_says_hi(store)
    bot, _ = responder(chatwoot, store, keystore=None, fallback=FakeLlm(status=401))
    assert await bot.respond(KEY) is Outcome.HANDED_OFF
    assert chatwoot.private_notes() == [f"{FALLBACK_REJECTED_NOTE} Provider error: AuthenticationError."]


async def test_other_llm_failures_hand_off_without_a_note(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    await customer_says_hi(store)
    bot, _ = responder(chatwoot, store, keystore=None, fallback=FakeLlm(status=500))
    assert await bot.respond(KEY) is Outcome.HANDED_OFF
    assert chatwoot.private_notes() == []


async def test_failed_private_note_still_opens(chatwoot: FakeChatwoot, store: ValkeyStore) -> None:
    original = chatwoot.handler

    def notes_fail(request):
        if request.url.path.endswith("/messages") and json.loads(request.content)["private"]:
            return httpx.Response(500)
        return original(request)

    chatwoot.handler = notes_fail
    bot, _ = responder(chatwoot, store, keystore=None)
    assert await bot.hand_off(KEY, reason="test", note="note") is Outcome.HANDED_OFF
    assert chatwoot.writes()[-1] == "/toggle_status"
