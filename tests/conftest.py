from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import LIMITS, FakeChatwoot


@pytest.fixture
async def valkey() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def store(valkey: FakeAsyncRedis) -> ValkeyStore:
    return ValkeyStore(valkey, LIMITS)


@pytest.fixture
def chatwoot() -> FakeChatwoot:
    return FakeChatwoot()
