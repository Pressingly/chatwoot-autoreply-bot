"""One reply turn for one conversation: answer from the transcript, or hand off to a human."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum

from chatwoot_autoreply_bot.chatwoot import ChatwootClient, ChatwootError
from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry
from chatwoot_autoreply_bot.llm import LlmClient, LlmError, build_messages, decide
from chatwoot_autoreply_bot.store import ValkeyStore

log = logging.getLogger(__name__)

BOT_STATUS = "pending"


class Outcome(StrEnum):
    REPLIED = "replied"
    HANDED_OFF = "handed_off"
    SKIPPED = "skipped"


def unanswered_customer_messages(transcript: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    unanswered: list[TranscriptEntry] = []
    for entry in reversed(transcript):
        if entry.role != "user":
            break
        unanswered.append(entry)
    return unanswered[::-1]


def asks_for_human(messages: Sequence[TranscriptEntry], phrases: Sequence[str]) -> bool:
    return any(phrase in message.content.lower() for message in messages for phrase in phrases)


class Responder:
    def __init__(
        self,
        *,
        chatwoot: ChatwootClient,
        llm: LlmClient,
        store: ValkeyStore,
        prompt_for: Callable[[int], str],
        handoff_message: str,
        handoff_phrases: Sequence[str],
    ) -> None:
        self._chatwoot = chatwoot
        self._llm = llm
        self._store = store
        self._prompt_for = prompt_for
        self._handoff_message = handoff_message
        self._handoff_phrases = handoff_phrases

    async def respond(self, key: ConversationKey) -> Outcome:
        if not await self._bot_owns(key):
            return Outcome.SKIPPED
        transcript = await self._store.transcript(key)
        unanswered = unanswered_customer_messages(transcript)
        if not unanswered:
            return Outcome.SKIPPED
        if asks_for_human(unanswered, self._handoff_phrases):
            return await self.hand_off(key, reason="customer_request")
        try:
            async with self._typing(key):
                completion = await self._llm.complete(build_messages(self._prompt_for(key.account_id), transcript))
        except LlmError as error:
            log.warning("llm failed account=%s conversation=%s error=%s", *_ids(key), error)
            return await self.hand_off(key, reason="llm_error")
        decision = decide(completion)
        if decision.handoff:
            return await self.hand_off(key, reason="llm_handoff")
        if not await self._bot_owns(key):
            return Outcome.SKIPPED
        message_id = await self._chatwoot.post_message(key, decision.text)
        if message_id is not None:
            await self._store.append(key, TranscriptEntry(message_id, "assistant", decision.text))
        log.info("replied account=%s conversation=%s", *_ids(key))
        return Outcome.REPLIED

    async def hand_off(self, key: ConversationKey, *, reason: str) -> Outcome:
        if await self._bot_released(key):
            return Outcome.SKIPPED
        try:
            await self._chatwoot.post_message(key, self._handoff_message)
        except ChatwootError as error:
            log.warning("handoff note failed account=%s conversation=%s error=%s", *_ids(key), error)
        await self._chatwoot.open_conversation(key)
        log.info("handed off account=%s conversation=%s reason=%s", *_ids(key), reason)
        return Outcome.HANDED_OFF

    async def _bot_owns(self, key: ConversationKey) -> bool:
        return await self._chatwoot.conversation_status(key) == BOT_STATUS

    async def _bot_released(self, key: ConversationKey) -> bool:
        try:
            return not await self._bot_owns(key)
        except ChatwootError:
            return False

    @asynccontextmanager
    async def _typing(self, key: ConversationKey):
        await self._set_typing(key, on=True)
        try:
            yield
        finally:
            await self._set_typing(key, on=False)

    async def _set_typing(self, key: ConversationKey, *, on: bool) -> None:
        try:
            await self._chatwoot.set_typing(key, on)
        except ChatwootError as error:
            log.debug("typing toggle failed account=%s conversation=%s error=%s", *_ids(key), error)


def _ids(key: ConversationKey) -> tuple[int, int]:
    return key.account_id, key.conversation_id
