"""Turn a Chatwoot Agent Bot webhook payload into the few facts the bot acts on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from chatwoot_autoreply_bot.digits import parse_digits

Role = Literal["user", "assistant"]

MESSAGE_CREATED = "message_created"
ROLE_BY_MESSAGE_TYPE: dict[str, Role] = {"incoming": "user", "outgoing": "assistant"}
ATTACHMENT_PLACEHOLDER = "[The customer sent an attachment]"


@dataclass(frozen=True)
class ConversationKey:
    account_id: int
    conversation_id: int


@dataclass(frozen=True)
class MessageEvent:
    event: str
    message_id: int
    key: ConversationKey
    message_type: str
    private: bool
    content: str
    has_attachments: bool
    conversation_status: str
    sender_type: str = ""


@dataclass(frozen=True)
class TranscriptEntry:
    message_id: int
    role: Role
    content: str


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return parse_digits(value)
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def positive_int(value: Any) -> int | None:
    number = _int(value)
    return number if number is not None and number > 0 else None


@dataclass(frozen=True)
class InboxRef:
    inbox_id: int
    account_id: int | None
    name: str


def parse_inbox(payload: Any) -> InboxRef | None:
    """The ``inbox: {id, name}`` Chatwoot puts on message webhooks (``Message#webhook_data``)."""
    payload = _dict(payload)
    inbox = _dict(payload.get("inbox"))
    inbox_id = positive_int(inbox.get("id"))
    if inbox_id is None:
        return None
    name = inbox.get("name")
    return InboxRef(
        inbox_id=inbox_id,
        account_id=positive_int(_dict(payload.get("account")).get("id")),
        name=name.strip() if isinstance(name, str) else "",
    )


def parse_message_event(payload: Any) -> MessageEvent | None:
    """Return the message event, or None for other events and payloads missing an id."""
    payload = _dict(payload)
    event = payload.get("event")
    if event not in ("message_created", "message_updated"):
        return None
    conversation = _dict(payload.get("conversation"))
    message_id = _int(payload.get("id"))
    account_id = _int(_dict(payload.get("account")).get("id"))
    conversation_id = _int(conversation.get("id"))
    if message_id is None or account_id is None or conversation_id is None:
        return None
    content = payload.get("content")
    return MessageEvent(
        event=event,
        message_id=message_id,
        key=ConversationKey(account_id, conversation_id),
        message_type=str(payload.get("message_type") or ""),
        private=payload.get("private") is True,
        content=content.strip() if isinstance(content, str) else "",
        has_attachments=bool(payload.get("attachments")),
        conversation_status=str(conversation.get("status") or ""),
        sender_type=str(_dict(payload.get("sender")).get("type") or ""),
    )


def transcript_entry(event: MessageEvent) -> TranscriptEntry | None:
    """The history line a created, public chat message adds, if any."""
    role = ROLE_BY_MESSAGE_TYPE.get(event.message_type)
    if event.event != MESSAGE_CREATED or event.private or role is None:
        return None
    content = event.content or (ATTACHMENT_PLACEHOLDER if event.has_attachments and role == "user" else "")
    if not content:
        return None
    return TranscriptEntry(event.message_id, role, content)


def is_human_agent_reply(event: MessageEvent) -> bool:
    """A public reply by a Chatwoot user (an agent), which answers every earlier customer message."""
    return (
        event.event == MESSAGE_CREATED
        and event.message_type == "outgoing"
        and not event.private
        and event.sender_type == "user"
    )


def should_reply(event: MessageEvent) -> bool:
    return (
        event.event == MESSAGE_CREATED
        and event.message_type == "incoming"
        and not event.private
        and event.conversation_status == "pending"
    )
