"""Which LLM answers a conversation: the inbox's pasted key, else the deployment-wide env key.

Clients are cached per inbox and key fingerprint. A client replaced by a new or
deleted key is closed once the last turn leasing it finishes, so a reconnect never
cuts a running call short. A stored key is used only while its host is still on
``LLM_BASE_URL_ALLOWLIST``; the env fallback is operator-set and not checked.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass

from chatwoot_autoreply_bot.keystore import InboxKeyStore, KeyUnreadable
from chatwoot_autoreply_bot.llm import LlmClient, LlmCredentials

log = logging.getLogger(__name__)

ClientFactory = Callable[[LlmCredentials], LlmClient]

KEYSTORE_ERROR_NOTE = "Auto-reply could not read its LLM key store, so a person needs to take this conversation."
FALLBACK_REJECTED_NOTE = "The deployment's LLM key (LLM_API_KEY) was rejected. Ask the operator to replace it."


class LlmUnavailable(Exception):
    """No usable LLM for this inbox; ``note`` tells the agents why."""

    def __init__(self, reason: str, note: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.note = note


@dataclass(frozen=True)
class LlmSource:
    client: LlmClient
    rejection_note: str


@dataclass
class _Lease:
    client: LlmClient
    fingerprint: str
    holders: int = 0
    retired: bool = False

    @property
    def idle_and_retired(self) -> bool:
        return self.retired and self.holders == 0


class LlmRouter:
    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        keystore: InboxKeyStore | None = None,
        fallback: LlmClient | None = None,
        connect_url: str | None = None,
        allowed_hosts: Collection[str] = frozenset(),
    ) -> None:
        self._client_factory = client_factory
        self._keystore = keystore
        self._fallback = fallback
        self._connect_url = connect_url
        self._allowed_hosts = frozenset(allowed_hosts)
        self._leases: dict[int, _Lease] = {}
        self._generations: dict[int, int] = {}

    @asynccontextmanager
    async def lease(self, inbox_id: int | None) -> AsyncIterator[LlmSource]:
        if inbox_id is None:
            yield self._fallback_source()
            return
        seen_generation = self._generation(inbox_id)
        credentials = await self._stored_credentials(inbox_id)
        if credentials is None:
            await self._forget(inbox_id)
            yield self._fallback_source()
            return
        if credentials.host not in self._allowed_hosts:
            await self._forget(inbox_id)
            log.warning("stored llm key host not allowed inbox=%s host=%s", inbox_id, credentials.host)
            raise LlmUnavailable("host_not_allowed", self._host_not_allowed_note())
        lease, replaced = self._acquire(inbox_id, credentials, seen_generation)
        try:
            await self._retire(replaced)
            yield LlmSource(lease.client, self._pasted_rejection_note())
        finally:
            lease.holders -= 1
            if lease.idle_and_retired:
                await _close_quietly(lease.client)

    async def aclose(self) -> None:
        for lease in list(self._leases.values()):
            await _close_quietly(lease.client)
        self._leases.clear()
        if self._fallback is not None:
            await _close_quietly(self._fallback)

    async def _stored_credentials(self, inbox_id: int | None) -> LlmCredentials | None:
        if self._keystore is None or inbox_id is None:
            return None
        try:
            return await self._keystore.credentials(inbox_id)
        except KeyUnreadable as error:
            log.error("stored llm key unreadable inbox=%s error=%s", inbox_id, error)
            raise LlmUnavailable("key_unreadable", self._unreadable_note()) from error
        except Exception as error:
            log.error("keystore read failed inbox=%s error=%s", inbox_id, type(error).__name__)
            raise LlmUnavailable("keystore_error", KEYSTORE_ERROR_NOTE) from error

    def _generation(self, inbox_id: int) -> int:
        return self._generations.get(inbox_id, 0)

    def _advance_generation(self, inbox_id: int) -> None:
        self._generations[inbox_id] = self._generation(inbox_id) + 1

    async def _forget(self, inbox_id: int) -> None:
        self._advance_generation(inbox_id)
        await self._retire(self._leases.pop(inbox_id, None))

    def _acquire(
        self, inbox_id: int, credentials: LlmCredentials, seen_generation: int
    ) -> tuple[_Lease, _Lease | None]:
        """Synchronous on purpose: no other turn can run between the lookup, the install and the hold.

        A turn whose keystore read predates a newer observed change gets a one-shot client instead
        of installing its possibly stale key over the newer one.
        """
        current = self._leases.get(inbox_id)
        if current is not None and current.fingerprint == credentials.fingerprint:
            current.holders += 1
            return current, None
        client = self._client_factory(credentials)
        if seen_generation < self._generation(inbox_id):
            return _Lease(client, credentials.fingerprint, holders=1, retired=True), None
        self._advance_generation(inbox_id)
        lease = _Lease(client, credentials.fingerprint, holders=1)
        self._leases[inbox_id] = lease
        return lease, current

    async def _retire(self, lease: _Lease | None) -> None:
        if lease is None:
            return
        lease.retired = True
        if lease.idle_and_retired:
            await _close_quietly(lease.client)

    def _fallback_source(self) -> LlmSource:
        if self._fallback is None:
            raise LlmUnavailable("no_llm_key", self._with_connect_hint("Auto-reply has no LLM key for this inbox."))
        return LlmSource(self._fallback, FALLBACK_REJECTED_NOTE)

    def _pasted_rejection_note(self) -> str:
        return self._with_connect_hint("LLM key rejected for this inbox.", verb="reconnect it")

    def _host_not_allowed_note(self) -> str:
        return self._with_connect_hint(
            "The stored LLM key for this inbox uses a provider host that is no longer allowed.", verb="reconnect it"
        )

    def _unreadable_note(self) -> str:
        return self._with_connect_hint("The stored LLM key for this inbox cannot be read.", verb="reconnect it")

    def _with_connect_hint(self, problem: str, verb: str = "connect one") -> str:
        if self._connect_url is None:
            return problem
        return f"{problem} An allowed admin can {verb} at {self._connect_url}."


async def _close_quietly(client: LlmClient) -> None:
    try:
        await client.aclose()
    except Exception as error:
        log.warning("llm client close failed error=%s", type(error).__name__)
