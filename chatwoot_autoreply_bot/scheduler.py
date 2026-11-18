"""Per-conversation debounce and serialisation of reply turns.

Every triggering message bumps the conversation's generation and starts a task that
sleeps for the debounce window. Only the task holding the latest generation goes on,
and it runs under the conversation's lock, so a burst of customer messages yields one
reply and two replies never run at once. Tasks live in this process: a restart drops
the ones still waiting, and those conversations stay pending until the customer
writes again.
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
        task = asyncio.create_task(self._run(key, generation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            async with self._store.lock(key):
                if await self._superseded(key, generation):
                    return
                await self._respond(key)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._hand_off_after_failure(key, error)

    async def _superseded(self, key: ConversationKey, generation: int) -> bool:
        return await self._store.generation(key) != generation

    async def _hand_off_after_failure(self, key: ConversationKey, error: Exception) -> None:
        log.warning(
            "reply failed account=%s conversation=%s error=%s",
            key.account_id,
            key.conversation_id,
            type(error).__name__,
        )
        try:
            await self._hand_off(key, reason="reply_error")
        except Exception as handoff_error:
            log.error(
                "handoff failed account=%s conversation=%s error=%s",
                key.account_id,
                key.conversation_id,
                type(handoff_error).__name__,
            )
