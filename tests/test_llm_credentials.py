from __future__ import annotations

import json

import httpx
import openai
import pytest

from chatwoot_autoreply_bot.llm import (
    PROBE_MESSAGES,
    LlmClient,
    LlmCredentials,
    LlmLimits,
    LlmRejected,
    check_credentials,
    probe_failure_message,
)
from tests.helpers import completion_response
from tests.test_llm import llm_with

CREDENTIALS = LlmCredentials("https://api.openai.com/v1", "m", "sk-1")
LIMITS = LlmLimits(timeout=5, max_retries=0)


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_key_or_model_rejection_is_its_own_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "key sk-secret is bad"}})

    with pytest.raises(LlmRejected) as raised:
        await llm_with(handler).complete([{"role": "user", "content": "Hi"}])
    assert "sk-secret" not in str(raised.value)


async def test_probe_sends_one_small_completion_with_the_reply_limits() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion_response(None))

    await llm_with(handler, max_tokens=20).probe()
    assert seen == [{"model": "test-model", "messages": PROBE_MESSAGES, "max_completion_tokens": 20}]


def status_error(status: int) -> Exception:
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "sk-echoed-key"}})
    return openai.AsyncOpenAI(api_key="k")._make_status_error_from_response(response)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(401), "rejected this API key"),
        (status_error(403), "rejected this API key"),
        (status_error(404), "does not know this model"),
        (status_error(400), "Check the model name"),
        (status_error(429), "HTTP 429"),
        (openai.APITimeoutError(httpx.Request("POST", "https://llm.test")), "did not answer in time"),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://llm.test")), "Could not reach"),
        (ValueError("sk-echoed-key"), "could not be checked"),
    ],
)
def test_probe_failure_messages_are_fixed(error: Exception, expected: str) -> None:
    message = probe_failure_message(error)
    assert expected in message
    assert "sk-echoed-key" not in message


class ClosingSpy:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.closed = 0

    def __call__(self, credentials: LlmCredentials, limits: LlmLimits) -> LlmClient:
        client = llm_with(self.handler)
        original = client.aclose

        async def aclose() -> None:
            self.closed += 1
            await original()

        client.aclose = aclose  # type: ignore[method-assign]
        return client


async def test_check_credentials_reports_a_rejection_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = ClosingSpy(lambda _: httpx.Response(401, json={}))
    monkeypatch.setattr(LlmClient, "for_credentials", spy)
    assert await check_credentials(CREDENTIALS, LIMITS) == "The provider rejected this API key."
    assert spy.closed == 1


async def test_check_credentials_accepts_a_working_key(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = ClosingSpy(lambda _: httpx.Response(200, json=completion_response("OK")))
    monkeypatch.setattr(LlmClient, "for_credentials", spy)
    assert await check_credentials(CREDENTIALS, LIMITS) is None
    assert spy.closed == 1


async def test_for_credentials_builds_a_client() -> None:
    client = LlmClient.for_credentials(CREDENTIALS, LlmLimits(timeout=5, max_retries=1, max_tokens=7))
    assert client._model == "m"
    assert client._max_tokens == 7
    assert client._client.max_retries == 1
    await client.aclose()


@pytest.mark.parametrize(
    ("base_url", "host"),
    [(None, "api.openai.com"), ("https://api.askii.ai/v1", "api.askii.ai"), ("", "api.openai.com")],
)
def test_credentials_host(base_url: str | None, host: str) -> None:
    assert LlmCredentials(base_url, "m", "k").host == host


def test_credentials_repr_hides_the_key_and_fingerprint_covers_every_field() -> None:
    credentials = LlmCredentials("https://api.openai.com/v1", "m", "sk-secret")
    assert "sk-secret" not in repr(credentials)
    fingerprints = {
        credentials.fingerprint,
        LlmCredentials("https://api.askii.ai/v1", "m", "sk-secret").fingerprint,
        LlmCredentials("https://api.openai.com/v1", "n", "sk-secret").fingerprint,
        LlmCredentials("https://api.openai.com/v1", "m", "sk-other").fingerprint,
    }
    assert len(fingerprints) == 4
