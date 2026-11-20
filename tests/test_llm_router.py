from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.keystore import KEYS_HASH, InboxKeyStore, KeyCipher
from chatwoot_autoreply_bot.llm import LlmClient, LlmCredentials
from chatwoot_autoreply_bot.llm_router import FALLBACK_REJECTED_NOTE, KEYSTORE_ERROR_NOTE, LlmRouter, LlmUnavailable
from tests.helpers import FERNET_KEY

CONNECT_URL = "https://chatwoot-autoreply.example.com/connect"
FIRST = LlmCredentials("https://api.openai.com/v1", "gpt-a", "sk-first-0000000000")
SECOND = LlmCredentials("https://api.openai.com/v1", "gpt-a", "sk-second-111111111")


class StubClient:
    def __init__(self, credentials: LlmCredentials | None = None, fail_close: bool = False) -> None:
        self.credentials = credentials
        self.closed = 0
        self.fail_close = fail_close

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("close failed")


class Factory:
    def __init__(self) -> None:
        self.built: list[StubClient] = []

    def __call__(self, credentials: LlmCredentials) -> LlmClient:
        client = StubClient(credentials)
        self.built.append(client)
        return client  # type: ignore[return-value]


@pytest.fixture
def keystore(valkey: FakeAsyncRedis) -> InboxKeyStore:
    return InboxKeyStore(valkey, KeyCipher(FERNET_KEY))


def router(keystore: InboxKeyStore | None, factory: Factory, fallback: StubClient | None = None) -> LlmRouter:
    return LlmRouter(
        client_factory=factory,
        keystore=keystore,
        fallback=fallback,  # type: ignore[arg-type]
        connect_url=CONNECT_URL,
    )


async def save(keystore: InboxKeyStore, inbox_id: int, credentials: LlmCredentials) -> None:
    await keystore.save(inbox_id, credentials, set_by="1234", set_at=1)


async def test_pasted_key_wins_over_the_fallback(keystore: InboxKeyStore) -> None:
    factory, fallback = Factory(), StubClient()
    await save(keystore, 3, FIRST)
    async with router(keystore, factory, fallback).lease(3) as source:
        assert source.client is factory.built[0]
        assert factory.built[0].credentials == FIRST
        assert "reconnect it at " + CONNECT_URL in source.rejection_note


async def test_inbox_without_a_key_uses_the_fallback(keystore: InboxKeyStore) -> None:
    factory, fallback = Factory(), StubClient()
    llms = router(keystore, factory, fallback)
    for inbox_id in (4, None):
        async with llms.lease(inbox_id) as source:
            assert source.client is fallback
            assert source.rejection_note == FALLBACK_REJECTED_NOTE
    assert factory.built == []


async def test_no_keystore_uses_the_fallback() -> None:
    fallback = StubClient()
    async with router(None, Factory(), fallback).lease(3) as source:
        assert source.client is fallback


async def test_no_key_and_no_fallback_is_unavailable_with_the_connect_hint(keystore: InboxKeyStore) -> None:
    with pytest.raises(LlmUnavailable) as raised:
        async with router(keystore, Factory()).lease(3):
            pass
    assert raised.value.reason == "no_llm_key"
    assert raised.value.note == (
        f"Auto-reply has no LLM key for this inbox. An allowed admin can connect one at {CONNECT_URL}."
    )


async def test_no_connect_url_leaves_the_hint_out() -> None:
    with pytest.raises(LlmUnavailable) as raised:
        async with LlmRouter(client_factory=Factory()).lease(3):
            pass
    assert raised.value.note == "Auto-reply has no LLM key for this inbox."


async def test_unreadable_key_is_unavailable_not_the_fallback(keystore: InboxKeyStore, valkey: FakeAsyncRedis) -> None:
    await valkey.hset(KEYS_HASH, "3", '{"sealed_key": "garbage", "model": "m"}')
    with pytest.raises(LlmUnavailable) as raised:
        async with router(keystore, Factory(), StubClient()).lease(3):
            pass
    assert raised.value.reason == "key_unreadable"
    assert CONNECT_URL in raised.value.note


async def test_keystore_outage_is_unavailable_not_the_fallback(valkey: FakeAsyncRedis) -> None:
    class DownKeystore(InboxKeyStore):
        async def credentials(self, inbox_id: int):
            raise ConnectionError("down")

    with pytest.raises(LlmUnavailable) as raised:
        async with router(DownKeystore(valkey, KeyCipher(FERNET_KEY)), Factory(), StubClient()).lease(3):
            pass
    assert raised.value.reason == "keystore_error"
    assert raised.value.note == KEYSTORE_ERROR_NOTE


async def test_client_is_cached_per_inbox_and_fingerprint(keystore: InboxKeyStore) -> None:
    factory = Factory()
    llms = router(keystore, factory)
    await save(keystore, 3, FIRST)
    await save(keystore, 4, FIRST)
    for inbox_id in (3, 3, 4):
        async with llms.lease(inbox_id):
            pass
    assert len(factory.built) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        SECOND,
        LlmCredentials("https://api.askii.ai/v1", "gpt-a", FIRST.api_key),
        LlmCredentials(None, "gpt-b", FIRST.api_key),
    ],
)
async def test_any_change_replaces_and_closes_the_client(keystore: InboxKeyStore, replacement: LlmCredentials) -> None:
    factory = Factory()
    llms = router(keystore, factory)
    await save(keystore, 3, FIRST)
    async with llms.lease(3):
        pass
    await save(keystore, 3, replacement)
    async with llms.lease(3) as source:
        assert source.client is factory.built[1]
    assert factory.built[0].closed == 1
    assert factory.built[1].closed == 0


async def test_replaced_client_closes_only_after_its_running_call(keystore: InboxKeyStore) -> None:
    factory = Factory()
    llms = router(keystore, factory)
    await save(keystore, 3, FIRST)
    async with llms.lease(3):
        await save(keystore, 3, SECOND)
        async with llms.lease(3):
            assert factory.built[0].closed == 0
        assert factory.built[0].closed == 0
    assert factory.built[0].closed == 1


async def test_deleted_key_retires_the_client(keystore: InboxKeyStore) -> None:
    factory, fallback = Factory(), StubClient()
    llms = router(keystore, factory, fallback)
    await save(keystore, 3, FIRST)
    async with llms.lease(3):
        pass
    await keystore.delete(3)
    async with llms.lease(3) as source:
        assert source.client is fallback
    assert factory.built[0].closed == 1


async def test_aclose_closes_cached_and_fallback_clients_even_if_one_fails(keystore: InboxKeyStore) -> None:
    factory, fallback = Factory(), StubClient(fail_close=True)
    llms = router(keystore, factory, fallback)
    await save(keystore, 3, FIRST)
    async with llms.lease(3):
        pass
    await llms.aclose()
    assert factory.built[0].closed == 1
    assert fallback.closed == 1


async def test_aclose_without_clients() -> None:
    await LlmRouter(client_factory=Factory()).aclose()


async def test_turns_racing_a_slow_close_share_one_new_client(keystore: InboxKeyStore) -> None:
    release = asyncio.Event()

    class SlowClose(StubClient):
        async def aclose(self) -> None:
            await release.wait()
            await super().aclose()

    built: list[StubClient] = []

    def factory(credentials: LlmCredentials) -> LlmClient:
        client = SlowClose(credentials) if not built else StubClient(credentials)
        built.append(client)
        return client  # type: ignore[return-value]

    llms = router(keystore, factory)  # type: ignore[arg-type]
    await save(keystore, 3, FIRST)
    async with llms.lease(3):
        pass
    await save(keystore, 3, SECOND)

    async def turn() -> LlmClient:
        async with llms.lease(3) as source:
            await asyncio.sleep(0)
            return source.client

    first, second = asyncio.create_task(turn()), asyncio.create_task(turn())
    await asyncio.sleep(0.01)
    release.set()
    assert await first is await second is built[1]
    assert len(built) == 2
    assert built[0].closed == 1
    await llms.aclose()
    assert built[1].closed == 1
