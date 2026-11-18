"""One reply turn for one conversation: answer from the transcript, or hand off to a human."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum

from chatwoot_autoreply_bot.chatwoot import ChatwootClient, ChatwootError
from chatwoot_autoreply_bot.events import ConversationKey, TranscriptEntry
from chatwoot_autoreply_bot.llm import LlmClient, LlmError, build_messages, decide
from chatwoot_autoreply_bot.store import ValkeyStore

log = logging.getLogger(__name__)

BOT_STATUS = "pending"
OPEN_ATTEMPTS = 3
OPEN_RETRY_SECONDS = 1.0

Sleep = Callable[[float], Awaitable[None]]


class Outcome(StrEnum):
    REPLIED = "replied"
    HANDED_OFF = "handed_off"
    HANDOFF_FAILED = "handoff_failed"
    SKIPPED = "skipped"


def unanswered_customer_messages(transcript: Sequence[TranscriptEntry], answered_through: int) -> list[TranscriptEntry]:
    return [entry for entry in transcript if entry.role == "user" and entry.message_id > answered_through]


def llm_context(transcript: Sequence[TranscriptEntry], unanswered: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """History first, then the unanswered messages, even ones older than a reply that did not see them."""
    pending = set(unanswered)
    return [entry for entry in transcript if entry not in pending] + list(unanswered)


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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._chatwoot = chatwoot
        self._llm = llm
        self._store = store
        self._prompt_for = prompt_for
        self._handoff_message = handoff_message
        self._handoff_phrases = handoff_phrases
        self._sleep = sleep

    async def respond(self, key: ConversationKey) -> Outcome:
        if not await self._bot_owns(key):
            return Outcome.SKIPPED
        transcript = await self._store.transcript(key)
        unanswered = unanswered_customer_messages(transcript, await self._store.answered_through(key))
        if not unanswered:
            return Outcome.SKIPPED
        through = unanswered[-1].message_id
        if asks_for_human(unanswered, self._handoff_phrases):
            return await self.hand_off(key, reason="customer_request", answered_through=through)
        messages = build_messages(self._prompt_for(key.account_id), llm_context(transcript, unanswered))
        try:
            async with self._typing(key):
                completion = await self._llm.complete(messages)
        except LlmError as error:
            log.warning("llm failed account=%s conversation=%s error=%s", *_ids(key), error)
            return await self.hand_off(key, reason="llm_error", answered_through=through)
        decision = decide(completion)
        if decision.handoff:
            return await self.hand_off(key, reason="llm_handoff", answered_through=through)
        if not await self._bot_owns(key):
            return Outcome.SKIPPED
        message_id = await self._chatwoot.post_message(key, decision.text)
        log.info("replied account=%s conversation=%s", *_ids(key))
        await self._record_reply(key, through, message_id, decision.text)
        return Outcome.REPLIED

    async def hand_off(self, key: ConversationKey, *, reason: str, answered_through: int | None = None) -> Outcome:
        """Post the note once and open the conversation. Never raises, so a caller never posts the note twice."""
        try:
            if await self._bot_released(key):
                return Outcome.SKIPPED
            await self._post_handoff_note(key)
            opened = await self._open(key)
        except Exception as error:
            log.error("handoff failed account=%s conversation=%s error=%s", *_ids(key), type(error).__name__)
            return Outcome.HANDOFF_FAILED
        if answered_through is not None:
            await self._advance_quietly(key, answered_through)
        if not opened:
            return Outcome.HANDOFF_FAILED
        log.info("handed off account=%s conversation=%s reason=%s", *_ids(key), reason)
        return Outcome.HANDED_OFF

    async def _post_handoff_note(self, key: ConversationKey) -> None:
        try:
            await self._chatwoot.post_message(key, self._handoff_message)
        except ChatwootError as error:
            log.warning("handoff note failed account=%s conversation=%s error=%s", *_ids(key), error)

    async def _open(self, key: ConversationKey) -> bool:
        for attempt in range(1, OPEN_ATTEMPTS + 1):
            try:
                await self._chatwoot.open_conversation(key)
                return True
            except ChatwootError as error:
                log.warning("open failed account=%s conversation=%s attempt=%s error=%s", *_ids(key), attempt, error)
            if attempt < OPEN_ATTEMPTS:
                await self._sleep(OPEN_RETRY_SECONDS * attempt)
        log.error("conversation left pending after handoff account=%s conversation=%s", *_ids(key))
        return False

    async def _record_reply(self, key: ConversationKey, through: int, message_id: int | None, text: str) -> None:
        """Best effort once the reply is out: a failure here must not turn into a handoff."""
        await self._advance_quietly(key, through)
        if message_id is None:
            return
        try:
            await self._store.append(key, TranscriptEntry(message_id, "assistant", text))
        except Exception as error:
            log.warning("reply not recorded account=%s conversation=%s error=%s", *_ids(key), type(error).__name__)

    async def _advance_quietly(self, key: ConversationKey, through: int) -> None:
        try:
            await self._store.advance_answered(key, through)
        except Exception as error:
            log.warning("answered mark failed account=%s conversation=%s error=%s", *_ids(key), type(error).__name__)

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
