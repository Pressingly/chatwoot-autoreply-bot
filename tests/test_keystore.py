from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.events import InboxRef
from chatwoot_autoreply_bot.keystore import (
    INBOXES_HASH,
    KEYS_HASH,
    Connection,
    InboxKeyStore,
    KeyCipher,
    KeyUnreadable,
    key_hint,
)
from chatwoot_autoreply_bot.llm import LlmCredentials
from tests.helpers import FERNET_KEY

API_KEY = "sk-test-0123456789abcdef"
CREDENTIALS = LlmCredentials(base_url="https://api.openai.com/v1", model="gpt-test", api_key=API_KEY)


@pytest.fixture
def keystore(valkey: FakeAsyncRedis) -> InboxKeyStore:
    return InboxKeyStore(valkey, KeyCipher(FERNET_KEY))


def test_cipher_round_trip() -> None:
    cipher = KeyCipher(FERNET_KEY)
    assert cipher.open(3, cipher.seal(3, CREDENTIALS)) == CREDENTIALS


def test_sealed_key_does_not_open_for_another_inbox() -> None:
    cipher = KeyCipher(FERNET_KEY)
    with pytest.raises(KeyUnreadable):
        cipher.open(4, cipher.seal(3, CREDENTIALS))


def test_sealed_key_does_not_open_with_another_encryption_key() -> None:
    sealed = KeyCipher(FERNET_KEY).seal(3, CREDENTIALS)
    with pytest.raises(KeyUnreadable):
        KeyCipher(Fernet.generate_key().decode()).open(3, sealed)


@pytest.mark.parametrize(
    "payload",
    [
        {"inbox_id": 3},
        {"inbox_id": 3, "base_url": None, "model": "m"},
        {"inbox_id": 3, "base_url": 5, "model": "m", "api_key": "k"},
        ["not", "a", "dict"],
    ],
)
def test_incomplete_sealed_payload_is_unreadable(payload: object) -> None:
    sealed = Fernet(FERNET_KEY).encrypt(json.dumps(payload).encode()).decode()
    with pytest.raises(KeyUnreadable):
        KeyCipher(FERNET_KEY).open(3, sealed)


async def test_editing_the_plaintext_base_url_cannot_redirect_the_key(
    keystore: InboxKeyStore, valkey: FakeAsyncRedis
) -> None:
    await keystore.save(3, CREDENTIALS, set_by="1234", set_at=1)
    record = json.loads(await valkey.hget(KEYS_HASH, "3"))
    record["base_url"] = "https://attacker.example/v1"
    await valkey.hset(KEYS_HASH, "3", json.dumps(record))
    assert await keystore.credentials(3) == CREDENTIALS


@pytest.mark.parametrize(("api_key", "hint"), [(API_KEY, "cdef"), ("short-key", "")])
def test_key_hint_only_for_long_keys(api_key: str, hint: str) -> None:
    assert key_hint(api_key) == hint


async def test_save_then_read_credentials(keystore: InboxKeyStore) -> None:
    await keystore.save(3, CREDENTIALS, set_by="1234", set_at=1_800_000_000)
    assert await keystore.credentials(3) == CREDENTIALS
    assert await keystore.credentials(4) is None


async def test_stored_bytes_never_contain_the_key(keystore: InboxKeyStore, valkey: FakeAsyncRedis) -> None:
    await keystore.save(3, CREDENTIALS, set_by="1234", set_at=1_800_000_000)
    stored = await valkey.hget(KEYS_HASH, "3")
    assert stored is not None
    assert API_KEY.encode() not in stored
    assert API_KEY[:-4].encode() not in stored


async def test_connections_show_metadata_only(keystore: InboxKeyStore) -> None:
    await keystore.save(5, CREDENTIALS, set_by="1234", set_at=1_800_000_000)
    await keystore.save(3, LlmCredentials(None, "m", "tiny"), set_by="77", set_at=1)
    connections = await keystore.connections()
    assert connections == [
        Connection(3, "api.openai.com", "m", "", "77", 1.0),
        Connection(5, "api.openai.com", "gpt-test", "cdef", "1234", 1_800_000_000.0),
    ]
    assert await keystore.connection(5) == connections[1]
    assert await keystore.connection(9) is None


async def test_delete(keystore: InboxKeyStore) -> None:
    await keystore.save(3, CREDENTIALS, set_by="1234", set_at=1)
    assert await keystore.delete(3)
    assert not await keystore.delete(3)
    assert await keystore.credentials(3) is None


@pytest.mark.parametrize("raw", ["not json", json.dumps({"model": "m"}), json.dumps(["x"])])
async def test_malformed_record_is_unreadable_and_hidden(
    keystore: InboxKeyStore, valkey: FakeAsyncRedis, raw: str
) -> None:
    await valkey.hset(KEYS_HASH, "3", raw)
    with pytest.raises(KeyUnreadable):
        await keystore.credentials(3)
    assert await keystore.connections() == []


async def test_record_with_odd_fields_still_lists(keystore: InboxKeyStore, valkey: FakeAsyncRedis) -> None:
    await valkey.hset(KEYS_HASH, "3", json.dumps({"sealed_key": "x", "model": "m", "base_url": 5, "set_at": "x"}))
    await valkey.hset(KEYS_HASH, "abc", json.dumps({"sealed_key": "x", "model": "m"}))
    assert await keystore.connections() == [Connection(3, "api.openai.com", "m", "", "", 0.0)]


async def test_inboxes_are_remembered(keystore: InboxKeyStore, valkey: FakeAsyncRedis) -> None:
    await keystore.remember_inbox(InboxRef(7, 1, "Support " + "x" * 300))
    await keystore.remember_inbox(InboxRef(3, None, "Website"))
    await valkey.hset(INBOXES_HASH, "abc", "{}")
    await valkey.hset(INBOXES_HASH, "9", "not json")
    await valkey.hset(INBOXES_HASH, "8", json.dumps({"account_id": "1"}))
    inboxes = await keystore.inboxes()
    assert [inbox.inbox_id for inbox in inboxes] == [3, 7, 8]
    assert inboxes[0] == InboxRef(3, None, "Website")
    assert len(inboxes[1].name) == 200
    assert inboxes[2] == InboxRef(8, None, "")
