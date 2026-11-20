"""The LLM leg: any OpenAI-compatible chat completions endpoint.

Handoff is signalled with a plain-text sentinel rather than a tool call, so it works
on every OpenAI-compatible endpoint, including ones without function calling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    DefaultAsyncHttpxClient,
    NotFoundError,
    PermissionDeniedError,
)

from chatwoot_autoreply_bot.events import TranscriptEntry

HANDOFF_SENTINEL = "[[HANDOFF]]"
HANDOFF_INSTRUCTION = (
    f"If the customer asks for a human, is upset, or needs something you cannot answer from what you know, "
    f"reply with exactly {HANDOFF_SENTINEL} and nothing else; a team member will take over."
)
REJECTIONS = (AuthenticationError, PermissionDeniedError, NotFoundError)
PROBE_MESSAGES = [{"role": "user", "content": "Reply with OK."}]


class LlmError(RuntimeError):
    """The LLM call failed or returned no usable text."""


class LlmRejected(LlmError):
    """The endpoint refused the key or the model; retrying cannot help."""


@dataclass(frozen=True)
class Decision:
    handoff: bool
    text: str = ""


@dataclass(frozen=True)
class LlmCredentials:
    base_url: str | None
    model: str
    api_key: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        material = "\n".join((self.base_url or "", self.model, self.api_key))
        return hashlib.sha256(material.encode()).hexdigest()

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "") if self.base_url else "api.openai.com"


@dataclass(frozen=True)
class LlmLimits:
    timeout: float
    max_retries: int
    max_tokens: int | None = None


def build_messages(system_prompt: str, transcript: Sequence[TranscriptEntry]) -> list[dict[str, str]]:
    system = {"role": "system", "content": f"{system_prompt}\n\n{HANDOFF_INSTRUCTION}"}
    return [system, *({"role": entry.role, "content": entry.content} for entry in transcript)]


def decide(completion: str) -> Decision:
    if HANDOFF_SENTINEL in completion:
        return Decision(handoff=True)
    text = completion.strip()
    return Decision(handoff=not text, text=text)


def no_redirect_http_client(timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """A redirect would re-send the transcript and the key to a host the base URL allowlist never checked."""
    return DefaultAsyncHttpxClient(timeout=timeout, follow_redirects=False, transport=transport)


class LlmClient:
    def __init__(self, client: AsyncOpenAI, model: str, max_tokens: int | None = None) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @classmethod
    def build(
        cls,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        timeout: float,
        max_retries: int,
        max_tokens: int | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> LlmClient:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=no_redirect_http_client(timeout, transport),
        )
        return cls(client, model, max_tokens)

    @classmethod
    def for_credentials(cls, credentials: LlmCredentials, limits: LlmLimits) -> LlmClient:
        return cls.build(
            api_key=credentials.api_key,
            base_url=credentials.base_url,
            model=credentials.model,
            timeout=limits.timeout,
            max_retries=limits.max_retries,
            max_tokens=limits.max_tokens,
        )

    async def aclose(self) -> None:
        await self._client.close()

    def _limits(self) -> dict[str, int]:
        return {} if self._max_tokens is None else {"max_completion_tokens": self._max_tokens}

    async def complete(self, messages: list[dict[str, str]]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, **self._limits()
            )
        except REJECTIONS as error:
            raise LlmRejected(type(error).__name__) from error
        except Exception as error:
            raise LlmError(type(error).__name__) from error
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LlmError("empty completion")
        return content

    async def probe(self) -> None:
        """One tiny completion, as the reply path would send it; raises the provider's exception unchanged."""
        await self._client.chat.completions.create(model=self._model, messages=PROBE_MESSAGES, **self._limits())


def probe_failure_message(error: Exception) -> str:
    """A fixed message per failure class; the provider's own text can echo the key, so it is never shown."""
    if isinstance(error, AuthenticationError | PermissionDeniedError):
        return "The provider rejected this API key."
    if isinstance(error, NotFoundError):
        return "The provider does not know this model, or the base URL path is wrong."
    if isinstance(error, BadRequestError):
        return "The provider refused the test request. Check the model name."
    if isinstance(error, APITimeoutError):
        return "The provider did not answer in time. Try again."
    if isinstance(error, APIConnectionError):
        return "Could not reach the provider at this base URL."
    if isinstance(error, APIStatusError):
        return f"The provider answered the test request with HTTP {error.status_code}."
    return "The key could not be checked. Try again."


async def check_credentials(credentials: LlmCredentials, limits: LlmLimits) -> str | None:
    """None when the key and model answer a test completion, else a message safe to show the admin."""
    client = LlmClient.for_credentials(credentials, limits)
    try:
        await client.probe()
    except Exception as error:
        return probe_failure_message(error)
    finally:
        await client.aclose()
    return None
