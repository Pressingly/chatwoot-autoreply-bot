from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.events import (
    ATTACHMENT_PLACEHOLDER,
    ConversationKey,
    TranscriptEntry,
    is_human_agent_reply,
    parse_message_event,
    should_reply,
    transcript_entry,
)
from tests.helpers import message_payload


def parsed(**kwargs):
    event = parse_message_event(message_payload(**kwargs))
    assert event is not None
    return event


def test_parses_ids_from_the_payload() -> None:
    event = parsed(message_id=11)
    assert event.key == ConversationKey(1, 42)
    assert event.message_id == 11


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "conversation_status_changed", "id": 42},
        {"event": "webwidget_triggered"},
        {**message_payload(), "account": None},
        {**message_payload(), "conversation": {"status": "pending"}},
        {**message_payload(), "id": True},
        [],
        "x",
    ],
)
def test_non_message_or_malformed_payloads_are_ignored(payload) -> None:
    assert parse_message_event(payload) is None


@pytest.mark.parametrize(
    ("kwargs", "replies"),
    [
        ({}, True),
        ({"status": "open"}, False),
        ({"status": "resolved"}, False),
        ({"status": "snoozed"}, False),
        ({"message_type": "outgoing"}, False),
        ({"message_type": "activity"}, False),
        ({"message_type": "template"}, False),
        ({"private": True}, False),
        ({"event": "message_updated"}, False),
    ],
)
def test_reply_trigger_matrix(kwargs: dict, replies: bool) -> None:
    assert should_reply(parsed(**kwargs)) is replies


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, TranscriptEntry(7, "user", "Hello")),
        ({"message_type": "outgoing", "content": " Hi there "}, TranscriptEntry(7, "assistant", "Hi there")),
        ({"status": "open"}, TranscriptEntry(7, "user", "Hello")),
        ({"private": True}, None),
        ({"message_type": "activity"}, None),
        ({"event": "message_updated"}, None),
        ({"content": None}, None),
        ({"content": "", "attachments": [{"id": 1}]}, TranscriptEntry(7, "user", ATTACHMENT_PLACEHOLDER)),
        ({"message_type": "outgoing", "content": None, "attachments": [{"id": 1}]}, None),
    ],
)
def test_transcript_matrix(kwargs: dict, expected: TranscriptEntry | None) -> None:
    assert transcript_entry(parsed(**kwargs)) == expected


@pytest.mark.parametrize("raw_id", ["²", "1" * 5000])
def test_unicode_or_huge_ids_are_ignored(raw_id: str) -> None:
    assert parse_message_event({**message_payload(), "id": raw_id}) is None


@pytest.mark.parametrize(
    ("kwargs", "human"),
    [
        ({"message_type": "outgoing", "sender_type": "user"}, True),
        ({"message_type": "outgoing", "sender_type": "agent_bot"}, False),
        ({"message_type": "outgoing", "sender_type": "user", "private": True}, False),
        ({"message_type": "incoming", "sender_type": "contact"}, False),
        ({"message_type": "outgoing", "sender_type": "user", "event": "message_updated"}, False),
    ],
)
def test_human_agent_reply(kwargs: dict, human: bool) -> None:
    sender_type = kwargs.pop("sender_type")
    payload = {**message_payload(**kwargs), "sender": {"id": 1, "type": sender_type}}
    event = parse_message_event(payload)
    assert event is not None
    assert is_human_agent_reply(event) is human
