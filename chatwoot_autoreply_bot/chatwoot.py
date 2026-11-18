"""The handful of Chatwoot REST calls an Agent Bot token is allowed to make."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from chatwoot_autoreply_bot.events import ConversationKey


class ChatwootError(RuntimeError):
    """A Chatwoot call failed or returned something unusable."""


class ChatwootClient:
    def __init__(self, http: httpx.AsyncClient, call_timeout_seconds: float) -> None:
        self._http = http
        self._call_timeout_seconds = call_timeout_seconds

    @classmethod
    def build(cls, base_url: str, bot_token: str, timeout_seconds: float) -> ChatwootClient:
        http = httpx.AsyncClient(
            base_url=base_url,
            headers={"api_access_token": bot_token, "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        return cls(http, timeout_seconds)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def conversation_status(self, key: ConversationKey) -> str:
        data = await self._request("GET", _conversation_path(key))
        status = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status, str):
            raise ChatwootError("conversation response has no status")
        return status

    async def set_typing(self, key: ConversationKey, on: bool) -> None:
        await self._request(
            "POST", f"{_conversation_path(key)}/toggle_typing_status", {"typing_status": "on" if on else "off"}
        )

    async def post_message(self, key: ConversationKey, content: str) -> int | None:
        data = await self._request(
            "POST",
            f"{_conversation_path(key)}/messages",
            {"content": content, "message_type": "outgoing", "private": False},
        )
        message_id = data.get("id") if isinstance(data, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def open_conversation(self, key: ConversationKey) -> None:
        await self._request("POST", f"{_conversation_path(key)}/toggle_status", {"status": "open"})

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            async with asyncio.timeout(self._call_timeout_seconds):
                response = await self._http.request(method, path, json=body)
        except (httpx.HTTPError, TimeoutError) as error:
            raise ChatwootError(f"{method} {path}: {type(error).__name__}") from error
        if response.is_error:
            raise ChatwootError(f"{method} {path}: HTTP {response.status_code}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None


def _conversation_path(key: ConversationKey) -> str:
    return f"/api/v1/accounts/{key.account_id}/conversations/{key.conversation_id}"
