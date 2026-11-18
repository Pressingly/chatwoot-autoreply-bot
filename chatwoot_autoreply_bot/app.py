"""HTTP surface: ``POST /webhook`` for Chatwoot and ``GET /healthz`` for the container probe.

The webhook only verifies, dedupes, records and schedules, then answers: Chatwoot
gives a bot webhook five seconds, so the LLM never runs in the request path.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from chatwoot_autoreply_bot.chatwoot import ChatwootClient
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.digits import parse_digits
from chatwoot_autoreply_bot.events import (
    is_human_agent_reply,
    parse_message_event,
    should_reply,
    transcript_entry,
)
from chatwoot_autoreply_bot.llm import LlmClient
from chatwoot_autoreply_bot.responder import Responder
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.signature import verify
from chatwoot_autoreply_bot.store import StoreLimits, ValkeyStore

log = logging.getLogger(__name__)

Clock = Callable[[], float]
VALKEY_TIMEOUT_SECONDS = 2.0


class BodyTooLarge(Exception):
    """The request body exceeds the configured limit."""


@dataclass
class Services:
    store: ValkeyStore
    scheduler: ReplyScheduler
    closers: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.scheduler.aclose()
        for close in self.closers:
            await close()


def store_limits(settings: Settings) -> StoreLimits:
    return StoreLimits(
        dedupe_ttl_seconds=settings.dedupe_ttl_seconds,
        transcript_max_messages=settings.transcript_max_messages,
        transcript_ttl_seconds=settings.transcript_ttl_seconds,
        lock_ttl_seconds=settings.lock_ttl_seconds,
    )


def build_services(settings: Settings) -> Services:
    valkey = Redis.from_url(
        settings.valkey_url,
        socket_timeout=VALKEY_TIMEOUT_SECONDS,
        socket_connect_timeout=VALKEY_TIMEOUT_SECONDS,
    )
    store = ValkeyStore(valkey, store_limits(settings))
    chatwoot = ChatwootClient.build(
        settings.chatwoot_internal_url, settings.chatwoot_bot_token, settings.chatwoot_timeout_seconds
    )
    llm = LlmClient.build(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        max_tokens=settings.llm_max_tokens,
    )
    responder = Responder(
        chatwoot=chatwoot,
        llm=llm,
        store=store,
        prompt_for=settings.prompt_for,
        handoff_message=settings.handoff_message,
        handoff_phrases=settings.handoff_phrases,
        llm_budget_seconds=settings.llm_budget_seconds,
    )
    scheduler = ReplyScheduler(
        store=store,
        respond=responder.respond,
        hand_off=responder.hand_off,
        debounce_seconds=settings.debounce_seconds,
    )
    return Services(store, scheduler, [chatwoot.aclose, llm.aclose, valkey.aclose])


async def record_and_schedule(services: Services, payload: Any) -> None:
    event = parse_message_event(payload)
    if event is None:
        return
    entry = transcript_entry(event)
    if entry is not None:
        await services.store.append(event.key, entry)
    if is_human_agent_reply(event):
        await services.store.advance_answered(event.key, event.message_id)
    if should_reply(event):
        await services.scheduler.schedule(event.key)


def create_app(
    settings: Settings,
    services_factory: Callable[[Settings], Services] = build_services,
    clock: Clock = time.time,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = services_factory(settings)
        app.state.services = services
        await _resume_quietly(services)
        try:
            yield
        finally:
            await services.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        try:
            await request.app.state.services.store.ping()
        except Exception as error:
            log.warning("health check failed error=%s", type(error).__name__)
            return _status(503, "valkey unavailable")
        return _status(200, "ok")

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        try:
            body = await read_limited_body(request, settings.max_body_bytes)
        except BodyTooLarge:
            return _status(413, "body too large")
        signed = verify(
            settings.chatwoot_bot_secret,
            request.headers.get("X-Chatwoot-Timestamp"),
            request.headers.get("X-Chatwoot-Signature"),
            body,
            now=clock(),
            max_skew_seconds=settings.signature_max_skew_seconds,
        )
        if not signed:
            return _status(401, "invalid signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return _status(400, "invalid json")
        return await _accept(request.app.state.services, request.headers.get("X-Chatwoot-Delivery"), payload)

    return app


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Read the body before it is authenticated, refusing anything over ``max_bytes`` without buffering it."""
    declared = parse_digits(request.headers.get("content-length", ""))
    if declared is not None and declared > max_bytes:
        raise BodyTooLarge
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def _resume_quietly(services: Services) -> None:
    try:
        await services.scheduler.resume()
    except Exception as error:
        log.warning("resume failed error=%s", type(error).__name__)


async def _accept(services: Services, delivery_id: str | None, payload: Any) -> JSONResponse:
    try:
        if delivery_id and not await services.store.claim_delivery(delivery_id):
            return _status(200, "duplicate")
        await record_and_schedule(services, payload)
    except Exception as error:
        log.error("webhook processing failed error=%s", type(error).__name__)
        await _release_quietly(services, delivery_id)
        return _status(500, "retry")
    return _status(200, "accepted")


async def _release_quietly(services: Services, delivery_id: str | None) -> None:
    if not delivery_id:
        return
    try:
        await services.store.release_delivery(delivery_id)
    except Exception as error:
        log.warning("delivery release failed error=%s", type(error).__name__)


def _status(code: int, status: str) -> JSONResponse:
    return JSONResponse({"status": status}, status_code=code)
