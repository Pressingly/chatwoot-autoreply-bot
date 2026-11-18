"""Per-conversation debounce and serialisation of reply turns.

Every triggering message bumps the conversation's generation (in Valkey) and starts a
task that sleeps for the debounce window. Only the task holding the latest generation
goes on, and it runs under the conversation's Valkey lock. Every step of a turn is
bounded (see ``Settings.lock_ttl_seconds``), so the lock outlives the turn. A burst of
messages yields one reply and two replies never overlap, across processes too.

Each scheduled conversation is marked outstanding until a turn for its latest
generation finishes. ``resume`` reschedules the marked ones at startup, so turns still
waiting or running when the process stopped are not lost.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from chatwoot_autoreply_bot.events import ConversationKey
from chatwoot_autoreply_bot.store import ValkeyStore

log = logging.getLogger(__name__)

Respond = Callable[[ConversationKey], Awaitable[object]]
HandOff = Callable[..., Awaitable[object]]
Sleep = Callable[[float], Awaitable[None]]


class ReplyScheduler:
    def __init__(
        self,
        *,
        store: ValkeyStore,
        respond: Respond,
        hand_off: HandOff,
        debounce_seconds: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._store = store
        self._respond = respond
        self._hand_off = hand_off
        self._debounce_seconds = debounce_seconds
        self._sleep = sleep
        self._tasks: set[asyncio.Task[None]] = set()

    async def schedule(self, key: ConversationKey) -> None:
        generation = await self._store.bump_generation(key)
        await self._store.mark_outstanding(key, generation)
        task = asyncio.create_task(self._run(key, generation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def resume(self) -> int:
        keys = await self._store.outstanding()
        for key in keys:
            await self.schedule(key)
        if keys:
            log.info("resumed outstanding turns count=%s", len(keys))
        return len(keys)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, key: ConversationKey, generation: int) -> None:
        try:
            await self._sleep(self._debounce_seconds)
            if await self._superseded(key, generation):
                return
            if not await self._locked_turn(key, generation):
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._hand_off_after_failure(key, error)
        await self._clear_quietly(key, generation)

    async def _locked_turn(self, key: ConversationKey, generation: int) -> bool:
        """Run the turn under the lock; False when a newer generation took it over."""
        finished = False
        try:
            async with self._store.lock(key):
                if await self._superseded(key, generation):
                    return False
                await self._respond(key)
                finished = True
        except Exception as error:
            if not finished:
                raise
            log.warning("lock release failed account=%s conversation=%s error=%s", *_ids(key), type(error).__name__)
        return True

    async def _superseded(self, key: ConversationKey, generation: int) -> bool:
        return await self._store.generation(key) != generation

    async def _hand_off_after_failure(self, key: ConversationKey, error: Exception) -> None:
        """The turn failed before any handoff; ``hand_off`` itself never raises or re-posts."""
        log.warning("reply failed account=%s conversation=%s error=%s", *_ids(key), type(error).__name__)
        try:
            await self._hand_off(key, reason="reply_error")
        except Exception as handoff_error:
            log.error("handoff failed account=%s conversation=%s error=%s", *_ids(key), type(handoff_error).__name__)

    async def _clear_quietly(self, key: ConversationKey, generation: int) -> None:
        try:
            await self._store.clear_outstanding(key, generation)
        except Exception as error:
            log.warning(
                "outstanding clear failed account=%s conversation=%s error=%s", *_ids(key), type(error).__name__
            )


def _ids(key: ConversationKey) -> tuple[int, int]:
    return key.account_id, key.conversation_id
