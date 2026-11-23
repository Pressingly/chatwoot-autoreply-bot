from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.fernet import Fernet

from chatwoot_autoreply_bot.chatwoot import ChatwootClient
from chatwoot_autoreply_bot.events import ConversationKey
from chatwoot_autoreply_bot.llm import LlmClient, LlmCredentials
from chatwoot_autoreply_bot.llm_router import LlmRouter
from chatwoot_autoreply_bot.store import StoreLimits

KEY = ConversationKey(account_id=1, conversation_id=42)
BASE = "/api/v1/accounts/1/conversations/42"
LIMITS = StoreLimits(dedupe_ttl_seconds=60, transcript_max_messages=5, transcript_ttl_seconds=600, lock_ttl_seconds=30)
INBOX_ID = 3
FERNET_KEY = Fernet.generate_key().decode()


def unused_factory(credentials: LlmCredentials) -> LlmClient:
    raise AssertionError("no per-inbox client expected")


def static_llms(client: LlmClient) -> LlmRouter:
    return LlmRouter(client_factory=unused_factory, fallback=client)


@dataclass
class FakeChatwoot:
    """A Chatwoot API stand-in served through ``httpx.MockTransport``."""

    status: str = "pending"
    statuses: list[str] = field(default_factory=list)
    fail_paths: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    next_message_id: int = 1000
    inbox_id: int | None = INBOX_ID

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        suffix = request.url.path.removeprefix(BASE)
        if suffix in self.fail_paths:
            return httpx.Response(500)
        if request.method == "GET" and suffix == "":
            status = self.statuses.pop(0) if self.statuses else self.status
            return httpx.Response(200, json={"id": KEY.conversation_id, "status": status, "inbox_id": self.inbox_id})
        if suffix == "/messages":
            self.next_message_id += 1
            return httpx.Response(200, json={"id": self.next_message_id})
        return httpx.Response(200)

    def client(self) -> ChatwootClient:
        http = httpx.AsyncClient(
            base_url="http://chatwoot:3000",
            headers={"api_access_token": "bot-token"},
            transport=httpx.MockTransport(self.handler),
        )
        return ChatwootClient(http, call_timeout_seconds=10)

    def calls(self) -> list[tuple[str, str, Any]]:
        return [
            (r.method, r.url.path.removeprefix(BASE), json.loads(r.content) if r.content else None)
            for r in self.requests
        ]

    def posted_messages(self) -> list[str]:
        return [body["content"] for body in self._message_bodies() if not body["private"]]

    def private_notes(self) -> list[str]:
        return [body["content"] for body in self._message_bodies() if body["private"]]

    def _message_bodies(self) -> list[dict[str, Any]]:
        return [body for method, path, body in self.calls() if method == "POST" and path == "/messages"]

    def writes(self) -> list[str]:
        return [path for method, path, _ in self.calls() if method == "POST"]


def completion_response(content: str | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


def message_payload(
    *,
    event: str = "message_created",
    message_id: int = 7,
    message_type: str = "incoming",
    private: bool = False,
    content: str | None = "Hello",
    status: str = "pending",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": event,
        "id": message_id,
        "account": {"id": KEY.account_id, "name": "Acme"},
        "conversation": {"id": KEY.conversation_id, "status": status, "messages": []},
        "inbox": {"id": 3, "name": "Website"},
        "message_type": message_type,
        "private": private,
        "content": content,
        "sender": {"id": 9, "type": "contact"},
    }
    if attachments is not None:
        payload["attachments"] = attachments
    return payload
