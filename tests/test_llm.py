from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from chatwoot_autoreply_bot.events import TranscriptEntry
from chatwoot_autoreply_bot.llm import (
    HANDOFF_INSTRUCTION,
    HANDOFF_SENTINEL,
    Decision,
    LlmClient,
    LlmCredentials,
    LlmError,
    LlmLimits,
    build_messages,
    decide,
)
from tests.helpers import completion_response


def llm_with(handler, max_tokens: int | None = None) -> LlmClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(api_key="k", base_url="https://llm.test/v1", http_client=http, max_retries=0)
    return LlmClient(client, "test-model", max_tokens)


def test_build_messages_appends_handoff_rule_and_transcript() -> None:
    messages = build_messages(
        "Acme only.", [TranscriptEntry(1, "user", "Hi"), TranscriptEntry(2, "assistant", "Hello")]
    )
    assert messages[0] == {"role": "system", "content": f"Acme only.\n\n{HANDOFF_INSTRUCTION}"}
    assert messages[1:] == [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("Our store opens at 9.", Decision(handoff=False, text="Our store opens at 9.")),
        (HANDOFF_SENTINEL, Decision(handoff=True)),
        (f"Sure, one moment. {HANDOFF_SENTINEL} Thanks", Decision(handoff=True)),
        ("   ", Decision(handoff=True)),
    ],
)
def test_decide(completion: str, expected: Decision) -> None:
    assert decide(completion) == expected


async def test_complete_sends_model_and_messages() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion_response("Hi!"))

    assert await llm_with(handler).complete([{"role": "user", "content": "Hi"}]) == "Hi!"
    assert seen[0]["model"] == "test-model"
    assert "max_completion_tokens" not in seen[0]


async def test_complete_sends_token_limit_when_set() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion_response("Hi!"))

    await llm_with(handler, max_tokens=50).complete([{"role": "user", "content": "Hi"}])
    assert seen[0]["max_completion_tokens"] == 50


async def test_provider_error_becomes_llm_error_without_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "echo of the customer's words"}})

    with pytest.raises(LlmError) as raised:
        await llm_with(handler).complete([{"role": "user", "content": "Hi"}])
    assert "customer" not in str(raised.value)


async def test_empty_completion_is_an_error() -> None:
    with pytest.raises(LlmError, match="empty"):
        await llm_with(lambda _: httpx.Response(200, json=completion_response(None))).complete([])


async def test_build_and_close() -> None:
    client = LlmClient.build(
        api_key="k", base_url="https://llm.test/v1", model="m", timeout=5, max_retries=1, max_tokens=None
    )
    await client.aclose()


async def test_a_redirect_is_never_followed_off_host() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://evil.example/v1/chat/completions"})

    client = LlmClient.build(
        api_key="k",
        base_url="https://llm.test/v1",
        model="m",
        timeout=5,
        max_retries=0,
        max_tokens=None,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LlmError):
        await client.complete([{"role": "user", "content": "secret transcript"}])
    await client.aclose()
    assert hosts == ["llm.test"]
    assert client._client._client.is_closed


def test_built_client_does_not_follow_redirects() -> None:
    client = LlmClient.for_credentials(LlmCredentials("https://api.openai.com/v1", "m", "k"), LlmLimits(7, 0))
    http = client._client._client
    assert http.follow_redirects is False
    assert http.timeout.read == 7
