from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.app import Services, create_app
from chatwoot_autoreply_bot.chatwoot import ConversationState
from chatwoot_autoreply_bot.events import InboxRef, parse_inbox, positive_int
from chatwoot_autoreply_bot.inbox_recorder import InboxRecorder
from chatwoot_autoreply_bot.keystore import InboxKeyStore, KeyCipher
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import FERNET_KEY, KEY, FakeChatwoot, message_payload
from tests.test_app import NOW, SETTINGS, Harness, SpyScheduler


@pytest.fixture
def keystore(valkey: FakeAsyncRedis) -> InboxKeyStore:
    return InboxKeyStore(valkey, KeyCipher(FERNET_KEY))


def test_parse_inbox_reads_the_message_webhook_shape() -> None:
    assert parse_inbox(message_payload()) == InboxRef(3, KEY.account_id, "Website")


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"inbox": "x"}, {"inbox": {"id": 0}}, {"inbox": {"id": "abc"}}, {"inbox": {"id": True}}],
)
def test_parse_inbox_ignores_payloads_without_an_inbox_id(payload: object) -> None:
    assert parse_inbox(payload) is None


def test_parse_inbox_tolerates_missing_name_and_account() -> None:
    assert parse_inbox({"inbox": {"id": "9", "name": 5}}) == InboxRef(9, None, "")


@pytest.mark.parametrize(("value", "expected"), [(3, 3), ("3", 3), (0, None), (-1, None), (None, None), (1.5, None)])
def test_positive_int(value: object, expected: int | None) -> None:
    assert positive_int(value) == expected


async def test_conversation_state_carries_the_inbox_id(chatwoot: FakeChatwoot) -> None:
    client = chatwoot.client()
    assert await client.conversation(KEY) == ConversationState("pending", 3)
    chatwoot.inbox_id = None
    assert await client.conversation(KEY) == ConversationState("pending", None)


async def test_private_note_is_posted_private(chatwoot: FakeChatwoot) -> None:
    await chatwoot.client().post_message(KEY, "note", private=True)
    assert chatwoot.calls() == [("POST", "/messages", {"content": "note", "message_type": "outgoing", "private": True})]


async def test_recorder_writes_new_or_changed_inboxes_once(keystore: InboxKeyStore, valkey: FakeAsyncRedis) -> None:
    writes: list[InboxRef] = []
    original = keystore.remember_inbox

    async def counting(inbox: InboxRef) -> None:
        writes.append(inbox)
        await original(inbox)

    keystore.remember_inbox = counting  # type: ignore[method-assign]
    recorder = InboxRecorder(keystore)
    for inbox in (InboxRef(3, 1, "Web"), InboxRef(3, 1, "Web"), None, InboxRef(3, 1, "Website")):
        await recorder.record(inbox)
    assert writes == [InboxRef(3, 1, "Web"), InboxRef(3, 1, "Website")]
    assert await keystore.inboxes() == [InboxRef(3, 1, "Website")]


async def test_recorder_never_raises_and_retries_later(valkey: FakeAsyncRedis) -> None:
    class FlakyKeystore(InboxKeyStore):
        failures = 1

        async def remember_inbox(self, inbox: InboxRef) -> None:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("down")
            await super().remember_inbox(inbox)

    keystore = FlakyKeystore(valkey, KeyCipher(FERNET_KEY))
    recorder = InboxRecorder(keystore)
    await recorder.record(InboxRef(3, 1, "Web"))
    assert await keystore.inboxes() == []
    await recorder.record(InboxRef(3, 1, "Web"))
    assert await keystore.inboxes() == [InboxRef(3, 1, "Web")]


async def test_recorder_gives_a_hung_keystore_a_short_window(valkey: FakeAsyncRedis) -> None:
    class HungKeystore(InboxKeyStore):
        async def remember_inbox(self, inbox: InboxRef) -> None:
            await asyncio.sleep(60)

    recorder = InboxRecorder(HungKeystore(valkey, KeyCipher(FERNET_KEY)), timeout_seconds=0.01)
    await recorder.record(InboxRef(3, 1, "Web"))


async def test_webhook_records_the_inbox_for_the_connect_page(store: ValkeyStore, keystore: InboxKeyStore) -> None:
    scheduler = SpyScheduler()
    services = Services(store, scheduler, keystore=keystore, inbox_recorder=InboxRecorder(keystore))  # type: ignore[arg-type]
    app = create_app(SETTINGS, lambda _: services, clock=lambda: NOW)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            response = await Harness(client, store, scheduler).deliver(message_payload())
    assert response.json() == {"status": "accepted"}
    assert await keystore.inboxes() == [InboxRef(3, KEY.account_id, "Website")]
    assert scheduler.scheduled == [KEY]


async def test_webhook_ack_survives_a_keystore_outage(store: ValkeyStore, valkey: FakeAsyncRedis) -> None:
    class DownKeystore(InboxKeyStore):
        async def remember_inbox(self, inbox: InboxRef) -> None:
            raise ConnectionError("down")

    keystore = DownKeystore(valkey, KeyCipher(FERNET_KEY))
    scheduler = SpyScheduler()
    services = Services(store, scheduler, keystore=keystore, inbox_recorder=InboxRecorder(keystore))  # type: ignore[arg-type]
    app = create_app(SETTINGS, lambda _: services, clock=lambda: NOW)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            response = await Harness(client, store, scheduler).deliver(message_payload())
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "accepted"}
