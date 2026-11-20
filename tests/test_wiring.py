from __future__ import annotations

import pytest

from chatwoot_autoreply_bot import __main__ as entry
from chatwoot_autoreply_bot.app import build_services
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.keystore import InboxKeyStore
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import FERNET_KEY
from tests.test_config import REQUIRED_ENV


async def test_build_services_wires_real_clients() -> None:
    services = build_services(Settings.from_env(REQUIRED_ENV))
    assert isinstance(services.store, ValkeyStore)
    assert isinstance(services.scheduler, ReplyScheduler)
    assert len(services.closers) == 3
    pool_kwargs = services.store._client.connection_pool.connection_kwargs
    assert pool_kwargs["socket_timeout"] == 2.0
    assert pool_kwargs["socket_connect_timeout"] == 2.0
    assert services.keystore is None
    assert services.inbox_recorder is None
    await services.aclose()


async def test_build_services_wires_the_keystore_and_connect_page() -> None:
    env = {
        **REQUIRED_ENV,
        "CONNECT_ADMIN_MPASS_IDS": "1234",
        "KEY_ENCRYPTION_KEY": FERNET_KEY,
        "KEYSTORE_VALKEY_URL": "redis://keystore:6379/0",
        "PUBLIC_BASE_URL": "https://bot.example.com",
        "CONNECT_EDGE_SECRET": "e" * 32,
    }
    settings = Settings.from_env(env)
    services = build_services(settings)
    assert isinstance(services.keystore, InboxKeyStore)
    assert services.inbox_recorder is not None
    assert len(services.closers) == 4
    keystore_pool = services.keystore._client.connection_pool
    assert keystore_pool.connection_kwargs["host"] == "keystore"
    assert keystore_pool.connection_kwargs["socket_timeout"] == 2.0
    router = services.scheduler._respond.__self__._llms
    assert router._connect_url == "https://bot.example.com/connect"
    assert router._fallback is not None
    await services.aclose()


async def test_build_services_without_an_env_llm() -> None:
    env = {name: value for name, value in REQUIRED_ENV.items() if name not in ("LLM_API_KEY", "LLM_MODEL")} | {
        "KEY_ENCRYPTION_KEY": FERNET_KEY,
        "KEYSTORE_VALKEY_URL": "redis://keystore:6379/0",
    }
    services = build_services(Settings.from_env(env))
    assert services.scheduler._respond.__self__._llms._fallback is None
    await services.aclose()


def test_main_exits_on_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="CHATWOOT_BOT_TOKEN"):
        entry.main()


def test_main_runs_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[dict] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    entry.main()
    assert calls == [{"host": "0.0.0.0", "port": 8000, "workers": 1, "access_log": False}]


async def test_build_services_passes_the_configured_time_limits() -> None:
    settings = Settings.from_env(
        {**REQUIRED_ENV, "LLM_TIMEOUT_SECONDS": "7", "LLM_MAX_RETRIES": "1", "CHATWOOT_TIMEOUT_SECONDS": "4"}
    )
    services = build_services(settings)
    responder = services.scheduler._respond.__self__
    assert responder._llm_budget_seconds == settings.llm_budget_seconds == 7 * 2 + 10
    assert responder._chatwoot._call_timeout_seconds == 4
    assert services.store._limits.lock_ttl_seconds == settings.lock_ttl_seconds
    await services.aclose()
