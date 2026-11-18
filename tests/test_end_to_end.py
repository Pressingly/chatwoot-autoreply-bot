from __future__ import annotations

import asyncio
import json

import httpx

from chatwoot_autoreply_bot.app import Services, create_app
from chatwoot_autoreply_bot.responder import Responder
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.signature import sign
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import FakeChatwoot, message_payload
from tests.test_app import NOW, SECRET, SETTINGS
from tests.test_responder import FakeLlm


async def test_burst_of_customer_messages_gets_one_reply_with_full_context(
    store: ValkeyStore, chatwoot: FakeChatwoot
) -> None:
    llm = FakeLlm("We open at 9.")
    responder = Responder(
        chatwoot=chatwoot.client(),
        llm=llm.client(),
        store=store,
        prompt_for=lambda _: "Acme support.",
        handoff_message="handoff",
        handoff_phrases=(),
    )
    scheduler = ReplyScheduler(
        store=store, respond=responder.respond, hand_off=responder.hand_off, debounce_seconds=0.05
    )
    app = create_app(SETTINGS, lambda _: Services(store, scheduler), clock=lambda: NOW)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            for message_id, content in enumerate(["Hi", "Are you open", "on Sunday?"], start=1):
                raw = json.dumps(message_payload(message_id=message_id, content=content)).encode()
                headers = {
                    "X-Chatwoot-Timestamp": str(NOW),
                    "X-Chatwoot-Signature": sign(SECRET, str(NOW), raw),
                    "X-Chatwoot-Delivery": f"d-{message_id}",
                }
                assert (await client.post("/webhook", content=raw, headers=headers)).status_code == 200
                await asyncio.sleep(0.01)
            await scheduler.drain()

    assert len(llm.requests) == 1
    assert [m["content"] for m in llm.requests[0]["messages"][1:]] == ["Hi", "Are you open", "on Sunday?"]
    assert chatwoot.posted_messages() == ["We open at 9."]
