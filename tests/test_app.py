from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.app import Services, create_app
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry
from chatwoot_autoreply_bot.signature import sign
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import KEY, LIMITS, message_payload

SECRET = "bot-secret"
NOW = 1_800_000_000
SETTINGS = Settings(
    chatwoot_bot_token="token",
    chatwoot_bot_secret=SECRET,
    valkey_url="redis://unused",
    llm_api_key="key",
    llm_model="model",
)


class SpyScheduler:
    def __init__(self, fail: bool = False) -> None:
        self.scheduled: list[ConversationKey] = []
        self.fail = fail
        self.closed = False

    async def schedule(self, key: ConversationKey) -> None:
        if self.fail:
            raise ConnectionError("valkey down")
        self.scheduled.append(key)

    async def aclose(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, client: httpx.AsyncClient, store: ValkeyStore, scheduler: SpyScheduler) -> None:
        self.client = client
        self.store = store
        self.scheduler = scheduler

    async def deliver(
        self,
        payload: Any,
        *,
        delivery: str | None = "d-1",
        timestamp: int = NOW,
        secret: str = SECRET,
        body: bytes | None = None,
    ) -> httpx.Response:
        raw = body if body is not None else json.dumps(payload).encode()
        headers = {
            "X-Chatwoot-Timestamp": str(timestamp),
            "X-Chatwoot-Signature": sign(secret, str(timestamp), raw),
            "Content-Type": "application/json",
        }
        if delivery:
            headers["X-Chatwoot-Delivery"] = delivery
        return await self.client.post("/webhook", content=raw, headers=headers)


@pytest.fixture
def scheduler() -> SpyScheduler:
    return SpyScheduler()


@pytest.fixture
async def harness(store: ValkeyStore, scheduler: SpyScheduler) -> AsyncIterator[Harness]:
    app = create_app(SETTINGS, lambda _: Services(store, scheduler), clock=lambda: NOW)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
            yield Harness(client, store, scheduler)
    assert scheduler.closed


async def test_healthz(harness: Harness) -> None:
    response = await harness.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_no_generated_docs(harness: Harness, path: str) -> None:
    assert (await harness.client.get(path)).status_code == 404


async def test_webhook_is_post_only(harness: Harness) -> None:
    assert (await harness.client.get("/webhook")).status_code == 405


async def test_pending_incoming_message_is_recorded_and_scheduled(harness: Harness) -> None:
    response = await harness.deliver(message_payload())
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert await harness.store.transcript(KEY) == [TranscriptEntry(7, "user", "Hello")]
    assert harness.scheduler.scheduled == [KEY]


async def test_agent_reply_is_recorded_but_not_scheduled(harness: Harness) -> None:
    response = await harness.deliver(message_payload(message_type="outgoing", content="Agent here", status="open"))
    assert response.status_code == 200
    assert await harness.store.transcript(KEY) == [TranscriptEntry(7, "assistant", "Agent here")]
    assert harness.scheduler.scheduled == []


async def test_other_events_are_acknowledged_and_ignored(harness: Harness) -> None:
    response = await harness.deliver({"event": "conversation_status_changed", "id": 42})
    assert response.status_code == 200
    assert harness.scheduler.scheduled == []


async def test_private_note_is_ignored(harness: Harness) -> None:
    await harness.deliver(message_payload(private=True))
    assert await harness.store.transcript(KEY) == []
    assert harness.scheduler.scheduled == []


async def test_duplicate_delivery_is_dropped(harness: Harness) -> None:
    await harness.deliver(message_payload(), delivery="same")
    response = await harness.deliver(message_payload(), delivery="same")
    assert response.json() == {"status": "duplicate"}
    assert harness.scheduler.scheduled == [KEY]


async def test_delivery_without_id_is_still_processed(harness: Harness) -> None:
    assert (await harness.deliver(message_payload(), delivery=None)).status_code == 200
    assert harness.scheduler.scheduled == [KEY]


@pytest.mark.parametrize(
    "kwargs",
    [{"secret": "wrong"}, {"timestamp": NOW - 301}, {"timestamp": NOW + 301}],
)
async def test_bad_signature_is_rejected(harness: Harness, kwargs: dict) -> None:
    response = await harness.deliver(message_payload(), **kwargs)
    assert response.status_code == 401
    assert harness.scheduler.scheduled == []
    assert await harness.store.claim_delivery("d-1")


async def test_tampered_body_is_rejected(harness: Harness) -> None:
    raw = json.dumps(message_payload()).encode()
    headers = {"X-Chatwoot-Timestamp": str(NOW), "X-Chatwoot-Signature": sign(SECRET, str(NOW), raw)}
    response = await harness.client.post("/webhook", content=raw.replace(b"Hello", b"Hullo"), headers=headers)
    assert response.status_code == 401


async def test_unsigned_request_is_rejected(harness: Harness) -> None:
    response = await harness.client.post("/webhook", json=message_payload())
    assert response.status_code == 401


async def test_signed_invalid_json_is_rejected(harness: Harness) -> None:
    assert (await harness.deliver(None, body=b"{not json")).status_code == 400


async def test_processing_failure_returns_500_and_frees_the_delivery_for_the_retry(
    store: ValkeyStore, valkey: FakeAsyncRedis
) -> None:
    failing = SpyScheduler(fail=True)
    app = create_app(SETTINGS, lambda _: Services(store, failing), clock=lambda: NOW)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            harness = Harness(client, store, failing)
            first = await harness.deliver(message_payload(), delivery="retry-me")
            assert first.status_code == 500
            failing.fail = False
            second = await harness.deliver(message_payload(), delivery="retry-me")
            assert second.status_code == 200
    assert failing.scheduled == [KEY]
    assert await store.transcript(KEY) == [TranscriptEntry(7, "user", "Hello")]


async def test_valkey_outage_returns_500(valkey: FakeAsyncRedis) -> None:
    class DownStore(ValkeyStore):
        async def claim_delivery(self, delivery_id: str) -> bool:
            raise ConnectionError("down")

        async def release_delivery(self, delivery_id: str) -> None:
            raise ConnectionError("down")

    store = DownStore(valkey, LIMITS)
    app = create_app(SETTINGS, lambda _: Services(store, SpyScheduler()), clock=lambda: NOW)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            response = await Harness(client, store, SpyScheduler()).deliver(message_payload())
    assert response.status_code == 500
