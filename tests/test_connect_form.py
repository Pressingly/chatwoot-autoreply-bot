from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.config import DEFAULT_LLM_BASE_URL_ALLOWLIST
from chatwoot_autoreply_bot.connect_form import (
    FormError,
    parse_api_key,
    parse_base_url,
    parse_connect_form,
    parse_inbox_id,
    parse_model,
)
from chatwoot_autoreply_bot.llm import LlmCredentials

HOSTS = DEFAULT_LLM_BASE_URL_ALLOWLIST


def test_valid_form() -> None:
    fields = {"inbox_id": " 3 ", "base_url": "https://API.openai.com/v1/", "model": " gpt-test ", "api_key": " sk-1 "}
    request = parse_connect_form(fields, HOSTS)
    assert request.inbox_id == 3
    assert request.credentials == LlmCredentials("https://api.openai.com/v1", "gpt-test", "sk-1")


@pytest.mark.parametrize("raw", ["", "0", "-1", "abc", "1.5", "²", "1" * 19])
def test_inbox_id_must_be_a_positive_integer(raw: str) -> None:
    with pytest.raises(FormError, match="Inbox id"):
        parse_inbox_id(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://api.openai.com/v1",
        "https://evil.example/v1",
        "https://api.openai.com.evil.example/v1",
        "https://api.openai.com@evil.example/v1",
        "https://user:pass@api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "https://api.openai.com:notaport/v1",
        "https://api.openai.com/v1?x=1",
        "https://api.openai.com/v1#frag",
        "https://api.openai.com/v\n1",
        "ftp://api.openai.com",
        "api.openai.com/v1",
        "https://" + "a" * 600,
    ],
)
def test_base_url_must_be_https_on_an_allowlisted_host(raw: str) -> None:
    with pytest.raises(FormError):
        parse_base_url(raw, HOSTS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.askii.ai", "https://api.askii.ai"),
        ("https://api.anthropic.com:443/v1/", "https://api.anthropic.com/v1"),
        ("https://api.sandbox.askii.ai/v1", "https://api.sandbox.askii.ai/v1"),
    ],
)
def test_allowed_base_urls_are_normalised(raw: str, expected: str) -> None:
    assert parse_base_url(raw, HOSTS) == expected


@pytest.mark.parametrize("raw", ["", "   ", "m" * 201, "gpt\n4"])
def test_model_bounds(raw: str) -> None:
    with pytest.raises(FormError, match="Model"):
        parse_model(raw)


def test_model_at_the_limit() -> None:
    assert parse_model("m" * 200) == "m" * 200


@pytest.mark.parametrize("raw", ["", "k" * 501, "sk 1", "sk\r\n1"])
def test_api_key_bounds(raw: str) -> None:
    with pytest.raises(FormError, match="API key"):
        parse_api_key(raw)


def test_api_key_at_the_limit() -> None:
    assert parse_api_key("k" * 500) == "k" * 500


def test_missing_fields_are_errors() -> None:
    with pytest.raises(FormError):
        parse_connect_form({}, HOSTS)
