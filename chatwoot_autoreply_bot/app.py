"""HTTP surface: ``POST /webhook`` for Chatwoot, ``GET /healthz`` for the container probe, and,
when ``CONNECT_ADMIN_MPASS_IDS`` is set, the ``/connect`` key page.

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

from chatwoot_autoreply_bot.bodies import BodyTooLarge, read_limited_body
from chatwoot_autoreply_bot.chatwoot import ChatwootClient
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.connect import ConnectDeps, CredentialCheck, connect_router
from chatwoot_autoreply_bot.events import (
    is_human_agent_reply,
    parse_inbox,
    parse_message_event,
    should_reply,
    transcript_entry,
)
from chatwoot_autoreply_bot.identity import AdminAllowlist
from chatwoot_autoreply_bot.inbox_recorder import InboxRecorder
from chatwoot_autoreply_bot.keystore import InboxKeyStore, KeyCipher
from chatwoot_autoreply_bot.llm import LlmClient, LlmCredentials, LlmLimits, check_credentials
from chatwoot_autoreply_bot.llm_router import LlmRouter
from chatwoot_autoreply_bot.responder import Responder
from chatwoot_autoreply_bot.scheduler import ReplyScheduler
from chatwoot_autoreply_bot.signature import verify
from chatwoot_autoreply_bot.store import StoreLimits, ValkeyStore

log = logging.getLogger(__name__)

Clock = Callable[[], float]
VALKEY_TIMEOUT_SECONDS = 2.0
KEY_CHECK_TIMEOUT_SECONDS = 15.0


@dataclass
class Services:
    store: ValkeyStore
    scheduler: ReplyScheduler
    closers: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)
    keystore: InboxKeyStore | None = None
    inbox_recorder: InboxRecorder | None = None

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


def llm_limits(settings: Settings) -> LlmLimits:
    return LlmLimits(settings.llm_timeout_seconds, settings.llm_max_retries, settings.llm_max_tokens)


def key_check_limits(settings: Settings) -> LlmLimits:
    return LlmLimits(min(settings.llm_timeout_seconds, KEY_CHECK_TIMEOUT_SECONDS), 0, settings.llm_max_tokens)


def valkey_client(url: str) -> Redis:
    return Redis.from_url(url, socket_timeout=VALKEY_TIMEOUT_SECONDS, socket_connect_timeout=VALKEY_TIMEOUT_SECONDS)


def fallback_llm(settings: Settings) -> LlmClient | None:
    if not settings.has_fallback_llm:
        return None
    credentials = LlmCredentials(settings.llm_base_url, str(settings.llm_model), str(settings.llm_api_key))
    return LlmClient.for_credentials(credentials, llm_limits(settings))


def build_keystore(settings: Settings) -> tuple[InboxKeyStore, Redis] | None:
    if not (settings.keystore_valkey_url and settings.key_encryption_key):
        return None
    client = valkey_client(settings.keystore_valkey_url)
    return InboxKeyStore(client, KeyCipher(settings.key_encryption_key)), client


def build_services(settings: Settings) -> Services:
    valkey = valkey_client(settings.valkey_url)
    store = ValkeyStore(valkey, store_limits(settings))
    built_keystore = build_keystore(settings)
    keystore = built_keystore[0] if built_keystore else None
    chatwoot = ChatwootClient.build(
        settings.chatwoot_internal_url, settings.chatwoot_bot_token, settings.chatwoot_timeout_seconds
    )
    limits = llm_limits(settings)
    llms = LlmRouter(
        client_factory=lambda credentials: LlmClient.for_credentials(credentials, limits),
        keystore=keystore,
        fallback=fallback_llm(settings),
        connect_url=settings.connect_url,
        allowed_hosts=settings.llm_base_url_allowlist,
    )
    responder = Responder(
        chatwoot=chatwoot,
        llms=llms,
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
    closers: list[Callable[[], Awaitable[Any]]] = [chatwoot.aclose, llms.aclose, valkey.aclose]
    if built_keystore:
        closers.append(built_keystore[1].aclose)
    recorder = InboxRecorder(keystore) if keystore else None
    return Services(store, scheduler, closers, keystore, recorder)


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
    if services.inbox_recorder is not None:
        await services.inbox_recorder.record(parse_inbox(payload))


def create_app(
    settings: Settings,
    services_factory: Callable[[Settings], Services] = build_services,
    clock: Clock = time.time,
    credential_check: CredentialCheck | None = None,
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
    if settings.connect_enabled:
        app.include_router(connect_router(_connect_deps(settings, clock, credential_check)))

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


def _connect_deps(settings: Settings, clock: Clock, credential_check: CredentialCheck | None) -> ConnectDeps:
    limits = key_check_limits(settings)

    async def check(credentials: LlmCredentials) -> str | None:
        return await check_credentials(credentials, limits)

    return ConnectDeps(
        allowlist=AdminAllowlist.build(settings.connect_admin_ids, settings.default_email_domain),
        edge_secret=str(settings.connect_edge_secret),
        trust_preferred_username=settings.connect_trust_preferred_username,
        public_origin=str(settings.public_base_url),
        allowed_hosts=frozenset(settings.llm_base_url_allowlist),
        check_credentials=credential_check or check,
        keystore_of=lambda request: request.app.state.services.keystore,
        clock=clock,
    )


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
