"""Per-inbox LLM keys, encrypted with Fernet, in a durable Valkey of their own.

The shared ``VALKEY_URL`` runs ``allkeys-lru`` and may evict, so keys live in
``KEYSTORE_VALKEY_URL`` (appendonly, noeviction). The sealed blob carries its inbox,
base URL and model as well as the key, so neither a ciphertext copied onto another
inbox nor an edited plaintext base URL can send the key anywhere else. The plaintext
fields only feed the Connect page listing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from redis.asyncio import Redis

from chatwoot_autoreply_bot.digits import parse_digits
from chatwoot_autoreply_bot.events import InboxRef
from chatwoot_autoreply_bot.llm import LlmCredentials

KEYS_HASH = "cwbot:llm-keys"
INBOXES_HASH = "cwbot:inboxes"
MIN_KEY_LENGTH_FOR_HINT = 12
MAX_INBOX_NAME_LENGTH = 200


class KeyUnreadable(Exception):
    """A stored key exists but cannot be decrypted or parsed."""


@dataclass(frozen=True)
class Connection:
    """What the Connect page may show about a stored key: never the key itself."""

    inbox_id: int
    host: str
    model: str
    key_hint: str
    set_by: str
    set_at: float


def key_hint(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= MIN_KEY_LENGTH_FOR_HINT else ""


class KeyCipher:
    def __init__(self, fernet_key: str) -> None:
        self._fernet = Fernet(fernet_key)

    def seal(self, inbox_id: int, credentials: LlmCredentials) -> str:
        plaintext = {
            "inbox_id": inbox_id,
            "base_url": credentials.base_url,
            "model": credentials.model,
            "api_key": credentials.api_key,
        }
        return self._fernet.encrypt(json.dumps(plaintext).encode()).decode()

    def open(self, inbox_id: int, sealed: str) -> LlmCredentials:
        try:
            data = json.loads(self._fernet.decrypt(sealed.encode()))
        except (InvalidToken, ValueError) as error:
            raise KeyUnreadable(type(error).__name__) from error
        if not _sealed_for(data, inbox_id):
            raise KeyUnreadable("sealed key belongs to another inbox or is incomplete")
        return LlmCredentials(base_url=data["base_url"], model=data["model"], api_key=data["api_key"])


def _sealed_for(data: Any, inbox_id: int) -> bool:
    return (
        isinstance(data, dict)
        and data.get("inbox_id") == inbox_id
        and isinstance(data.get("base_url"), str | None)
        and isinstance(data.get("model"), str)
        and isinstance(data.get("api_key"), str)
    )


class InboxKeyStore:
    def __init__(self, client: Redis, cipher: KeyCipher) -> None:
        self._client = client
        self._cipher = cipher

    async def save(self, inbox_id: int, credentials: LlmCredentials, *, set_by: str, set_at: float) -> None:
        record = {
            "base_url": credentials.base_url,
            "model": credentials.model,
            "sealed_key": self._cipher.seal(inbox_id, credentials),
            "key_hint": key_hint(credentials.api_key),
            "set_by": set_by,
            "set_at": set_at,
        }
        await self._client.hset(KEYS_HASH, str(inbox_id), json.dumps(record))

    async def delete(self, inbox_id: int) -> bool:
        return bool(await self._client.hdel(KEYS_HASH, str(inbox_id)))

    async def credentials(self, inbox_id: int) -> LlmCredentials | None:
        raw = await self._client.hget(KEYS_HASH, str(inbox_id))
        if raw is None:
            return None
        record = _record(raw)
        if record is None:
            raise KeyUnreadable("malformed record")
        return self._cipher.open(inbox_id, record["sealed_key"])

    async def connection(self, inbox_id: int) -> Connection | None:
        raw = await self._client.hget(KEYS_HASH, str(inbox_id))
        return None if raw is None else _connection(str(inbox_id), raw)

    async def connections(self) -> list[Connection]:
        rows = await self._client.hgetall(KEYS_HASH)
        connections = (_connection(field, value) for field, value in rows.items())
        return sorted(filter(None, connections), key=lambda connection: connection.inbox_id)

    async def remember_inbox(self, inbox: InboxRef) -> None:
        value = json.dumps({"account_id": inbox.account_id, "name": inbox.name[:MAX_INBOX_NAME_LENGTH]})
        await self._client.hset(INBOXES_HASH, str(inbox.inbox_id), value)

    async def inboxes(self) -> list[InboxRef]:
        rows = await self._client.hgetall(INBOXES_HASH)
        inboxes = (_inbox_ref(field, value) for field, value in rows.items())
        return sorted(filter(None, inboxes), key=lambda inbox: inbox.inbox_id)


def _text(raw: bytes | str) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


def _record(raw: bytes | str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    fields_ok = (
        isinstance(data, dict) and isinstance(data.get("sealed_key"), str) and isinstance(data.get("model"), str)
    )
    return data if fields_ok else None


def _connection(field: bytes | str, value: bytes | str) -> Connection | None:
    inbox_id = parse_digits(_text(field))
    record = _record(value)
    if inbox_id is None or record is None:
        return None
    set_at = record.get("set_at")
    return Connection(
        inbox_id=inbox_id,
        host=LlmCredentials(base_url=_base_url(record), model=record["model"], api_key="").host,
        model=record["model"],
        key_hint=str(record.get("key_hint") or ""),
        set_by=str(record.get("set_by") or ""),
        set_at=float(set_at) if isinstance(set_at, int | float) else 0.0,
    )


def _base_url(record: dict[str, Any]) -> str | None:
    base_url = record.get("base_url")
    return base_url if isinstance(base_url, str) else None


def _inbox_ref(field: bytes | str, value: bytes | str) -> InboxRef | None:
    inbox_id = parse_digits(_text(field))
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if inbox_id is None or not isinstance(data, dict):
        return None
    account_id = data.get("account_id")
    return InboxRef(
        inbox_id=inbox_id,
        account_id=account_id if isinstance(account_id, int) else None,
        name=str(data.get("name") or ""),
    )
