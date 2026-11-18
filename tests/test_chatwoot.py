from __future__ import annotations

import httpx
import pytest

from chatwoot_autoreply_bot.chatwoot import ChatwootClient, ChatwootError
from tests.helpers import KEY, FakeChatwoot


async def test_reads_conversation_status(chatwoot: FakeChatwoot) -> None:
    chatwoot.status = "open"
    assert await chatwoot.client().conversation_status(KEY) == "open"
    request = chatwoot.requests[0]
    assert request.url.path == "/api/v1/accounts/1/conversations/42"
    assert request.headers["api_access_token"] == "bot-token"


async def test_posts_outgoing_public_message(chatwoot: FakeChatwoot) -> None:
    message_id = await chatwoot.client().post_message(KEY, "Hi")
    assert message_id == 1001
    assert chatwoot.calls() == [("POST", "/messages", {"content": "Hi", "message_type": "outgoing", "private": False})]


async def test_typing_and_open(chatwoot: FakeChatwoot) -> None:
    client = chatwoot.client()
    await client.set_typing(KEY, True)
    await client.set_typing(KEY, False)
    await client.open_conversation(KEY)
    assert chatwoot.calls() == [
        ("POST", "/toggle_typing_status", {"typing_status": "on"}),
        ("POST", "/toggle_typing_status", {"typing_status": "off"}),
        ("POST", "/toggle_status", {"status": "open"}),
    ]


async def test_http_error_raises(chatwoot: FakeChatwoot) -> None:
    chatwoot.fail_paths.add("/messages")
    with pytest.raises(ChatwootError, match="HTTP 500"):
        await chatwoot.client().post_message(KEY, "Hi")


async def test_transport_error_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ChatwootClient(httpx.AsyncClient(base_url="http://chatwoot:3000", transport=httpx.MockTransport(refuse)))
    with pytest.raises(ChatwootError, match="ConnectError"):
        await client.conversation_status(KEY)


@pytest.mark.parametrize("response", [httpx.Response(200, json={}), httpx.Response(200, content=b"not json")])
async def test_status_missing_raises(response: httpx.Response) -> None:
    client = ChatwootClient(
        httpx.AsyncClient(base_url="http://chatwoot:3000", transport=httpx.MockTransport(lambda _: response))
    )
    with pytest.raises(ChatwootError, match="no status"):
        await client.conversation_status(KEY)


async def test_message_without_id_returns_none() -> None:
    client = ChatwootClient(
        httpx.AsyncClient(base_url="http://chatwoot:3000", transport=httpx.MockTransport(lambda _: httpx.Response(204)))
    )
    assert await client.post_message(KEY, "Hi") is None


async def test_build_sets_token_and_base_url() -> None:
    client = ChatwootClient.build("http://chatwoot:3000", "tok", 5)
    assert client._http.headers["api_access_token"] == "tok"
    assert str(client._http.base_url) == "http://chatwoot:3000"
    await client.aclose()


async def test_each_call_is_time_boxed() -> None:
    import asyncio

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    http = httpx.AsyncClient(base_url="http://chatwoot:3000", transport=httpx.MockTransport(hang))
    client = ChatwootClient(http, call_timeout_seconds=0.05)
    with pytest.raises(ChatwootError, match="TimeoutError"):
        await client.open_conversation(KEY)
