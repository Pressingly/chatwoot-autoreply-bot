from __future__ import annotations

import pytest

from chatwoot_autoreply_bot import __main__ as entry
from chatwoot_autoreply_bot.app import build_services
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.test_config import REQUIRED_ENV


async def test_build_services_wires_real_clients() -> None:
    services = build_services(Settings.from_env(REQUIRED_ENV))
    assert isinstance(services.store, ValkeyStore)
    assert isinstance(services.scheduler, ReplyScheduler)
    assert len(services.closers) == 3
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
