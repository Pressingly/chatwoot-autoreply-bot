"""The LLM leg: any OpenAI-compatible chat completions endpoint.

Handoff is signalled with a plain-text sentinel rather than a tool call, so it works
on every OpenAI-compatible endpoint, including ones without function calling.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI

from chatwoot_autoreply_bot.events import TranscriptEntry

HANDOFF_SENTINEL = "[[HANDOFF]]"
HANDOFF_INSTRUCTION = (
    f"If the customer asks for a human, is upset, or needs something you cannot answer from what you know, "
    f"reply with exactly {HANDOFF_SENTINEL} and nothing else; a team member will take over."
)


class LlmError(RuntimeError):
    """The LLM call failed or returned no usable text."""


@dataclass(frozen=True)
class Decision:
    handoff: bool
    text: str = ""


def build_messages(system_prompt: str, transcript: Sequence[TranscriptEntry]) -> list[dict[str, str]]:
    system = {"role": "system", "content": f"{system_prompt}\n\n{HANDOFF_INSTRUCTION}"}
    return [system, *({"role": entry.role, "content": entry.content} for entry in transcript)]


def decide(completion: str) -> Decision:
    if HANDOFF_SENTINEL in completion:
        return Decision(handoff=True)
    text = completion.strip()
    return Decision(handoff=not text, text=text)


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
    ) -> LlmClient:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        return cls(client, model, max_tokens)

    async def aclose(self) -> None:
        await self._client.close()

    def _limits(self) -> dict[str, int]:
        return {} if self._max_tokens is None else {"max_completion_tokens": self._max_tokens}

    async def complete(self, messages: list[dict[str, str]]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, **self._limits()
            )
        except Exception as error:
            raise LlmError(type(error).__name__) from error
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LlmError("empty completion")
        return content
