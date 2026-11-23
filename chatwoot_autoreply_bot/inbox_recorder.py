"""Remembers the inboxes seen in webhooks so the Connect page can list them.

Best effort and bounded: it writes only when an inbox is new or renamed, gives the
keystore a short window, and never raises, so the webhook ack never waits on it.
"""

from __future__ import annotations

import asyncio
import logging

from chatwoot_autoreply_bot.events import InboxRef
from chatwoot_autoreply_bot.keystore import InboxKeyStore

log = logging.getLogger(__name__)

RECORD_TIMEOUT_SECONDS = 0.5


class InboxRecorder:
    def __init__(self, keystore: InboxKeyStore, timeout_seconds: float = RECORD_TIMEOUT_SECONDS) -> None:
        self._keystore = keystore
        self._timeout_seconds = timeout_seconds
        self._recorded: dict[int, InboxRef] = {}

    async def record(self, inbox: InboxRef | None) -> None:
        if inbox is None or self._recorded.get(inbox.inbox_id) == inbox:
            return
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._keystore.remember_inbox(inbox)
        except Exception as error:
            log.warning("inbox not recorded inbox=%s error=%s", inbox.inbox_id, type(error).__name__)
            return
        self._recorded[inbox.inbox_id] = inbox
