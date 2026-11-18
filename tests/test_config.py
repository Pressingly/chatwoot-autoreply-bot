from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.config import DEFAULT_SYSTEM_PROMPT, ConfigError, Settings

REQUIRED_ENV = {
    "CHATWOOT_BOT_TOKEN": "token",
    "CHATWOOT_BOT_SECRET": "secret",
    "VALKEY_URL": "redis://valkey:6379/1",
    "LLM_API_KEY": "key",
    "LLM_MODEL": "gpt-test",
}


def settings(**extra: str) -> Settings:
    return Settings.from_env({**REQUIRED_ENV, **extra})


def test_defaults() -> None:
    result = settings()
    assert result.chatwoot_internal_url == "http://chatwoot:3000"
    assert result.llm_base_url is None
    assert result.llm_max_tokens is None
    assert result.debounce_seconds == 3.0
    assert result.prompt_for(1) == DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize("name", list(REQUIRED_ENV))
def test_missing_required_setting_fails_fast(name: str) -> None:
    env = {**REQUIRED_ENV, name: "  "}
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


def test_overrides_are_read() -> None:
    result = settings(
        CHATWOOT_INTERNAL_URL="http://chatwoot:3000/",
        LLM_BASE_URL="https://llm.example/v1",
        LLM_MAX_TOKENS="300",
        DEBOUNCE_SECONDS="0",
        SYSTEM_PROMPT="Be brief.",
        HANDOFF_PHRASES="Agent Please, ,human",
    )
    assert result.chatwoot_internal_url == "http://chatwoot:3000"
    assert result.llm_base_url == "https://llm.example/v1"
    assert result.llm_max_tokens == 300
    assert result.debounce_seconds == 0
    assert result.prompt_for(5) == "Be brief."
    assert result.handoff_phrases == ("agent please", "human")


def test_empty_handoff_phrases_disable_the_check() -> None:
    assert settings(HANDOFF_PHRASES="").handoff_phrases == ()


def test_account_prompt_overrides_default() -> None:
    result = settings(SYSTEM_PROMPT_ACCOUNT_7="Acme only.", SYSTEM_PROMPT_ACCOUNT_8="")
    assert result.prompt_for(7) == "Acme only."
    assert result.prompt_for(8) == DEFAULT_SYSTEM_PROMPT


def test_account_prompt_needs_numeric_id() -> None:
    with pytest.raises(ConfigError, match="SYSTEM_PROMPT_ACCOUNT_acme"):
        settings(SYSTEM_PROMPT_ACCOUNT_acme="x")


@pytest.mark.parametrize(
    ("name", "value"),
    [("DEBOUNCE_SECONDS", "-1"), ("LLM_TIMEOUT_SECONDS", "0"), ("PORT", "abc"), ("LLM_MAX_TOKENS", "-3")],
)
def test_invalid_numbers_fail(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        settings(**{name: value})


def test_lock_outlives_the_worst_case_reply() -> None:
    result = settings(LLM_TIMEOUT_SECONDS="30", LLM_MAX_RETRIES="2", CHATWOOT_TIMEOUT_SECONDS="10")
    assert result.lock_ttl_seconds > 30 * 3 + 10 * 6


@pytest.mark.parametrize("suffix", ["²", "1" * 5000])
def test_account_prompt_rejects_unicode_or_huge_ids(suffix: str) -> None:
    with pytest.raises(ConfigError, match="numeric"):
        settings(**{f"SYSTEM_PROMPT_ACCOUNT_{suffix}": "x"})


def test_turn_ends_before_the_lock_expires() -> None:
    result = settings()
    assert 0 < result.turn_timeout_seconds < result.lock_ttl_seconds
    assert settings(MAX_BODY_BYTES="2048").max_body_bytes == 2048
