from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.config import DEFAULT_LLM_BASE_URL_ALLOWLIST, ConfigError, Settings
from tests.helpers import FERNET_KEY
from tests.test_config import REQUIRED_ENV

BASE_ENV = {name: REQUIRED_ENV[name] for name in ("CHATWOOT_BOT_TOKEN", "CHATWOOT_BOT_SECRET", "VALKEY_URL")}
CONNECT_ENV = {
    "CONNECT_ADMIN_MPASS_IDS": " 1234 , Admin@Corp.Example ,, ",
    "KEY_ENCRYPTION_KEY": FERNET_KEY,
    "KEYSTORE_VALKEY_URL": "redis://keystore:6379/0",
    "PUBLIC_BASE_URL": "https://Chatwoot-Autoreply.example.com/",
    "DEFAULT_EMAIL_DOMAIN": " FOSS.Example ",
    "CONNECT_EDGE_SECRET": "e" * 32,
}


def test_llm_env_is_optional_with_connect() -> None:
    settings = Settings.from_env({**BASE_ENV, **CONNECT_ENV})
    assert settings.llm_api_key is None
    assert not settings.has_fallback_llm
    assert settings.connect_enabled and settings.keystore_enabled
    assert settings.connect_admin_ids == ("1234", "admin@corp.example")
    assert settings.public_base_url == "https://chatwoot-autoreply.example.com"
    assert settings.connect_url == "https://chatwoot-autoreply.example.com/connect"
    assert settings.default_email_domain == "foss.example"
    assert settings.llm_base_url_allowlist == DEFAULT_LLM_BASE_URL_ALLOWLIST


def test_env_llm_alone_keeps_working_with_connect_disabled() -> None:
    settings = Settings.from_env(REQUIRED_ENV)
    assert settings.has_fallback_llm
    assert not settings.connect_enabled
    assert not settings.keystore_enabled
    assert settings.connect_url is None


def test_secrets_stay_out_of_the_repr() -> None:
    settings = Settings.from_env({**REQUIRED_ENV, **CONNECT_ENV})
    assert FERNET_KEY not in repr(settings)
    assert REQUIRED_ENV["LLM_API_KEY"] + "'" not in repr(settings)


@pytest.mark.parametrize("missing", ["KEY_ENCRYPTION_KEY", "KEYSTORE_VALKEY_URL", "PUBLIC_BASE_URL"])
def test_allowlist_without_its_dependencies_refuses_to_start(missing: str) -> None:
    env = {**REQUIRED_ENV, **CONNECT_ENV, missing: ""}
    with pytest.raises(ConfigError, match=missing):
        Settings.from_env(env)


def test_bad_fernet_key_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="Fernet"):
        Settings.from_env({**REQUIRED_ENV, **CONNECT_ENV, "KEY_ENCRYPTION_KEY": "not-a-key"})


@pytest.mark.parametrize(
    "url",
    [
        "http://bot.example.com",
        "https://",
        "https://bot.example.com/connect",
        "https://u@bot.example.com",
        "https://bot.example.com?x=1",
        "https://bot.example.com#top",
        "https://bot.example.com:notaport",
    ],
)
def test_public_base_url_must_be_an_https_origin(url: str) -> None:
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        Settings.from_env({**REQUIRED_ENV, **CONNECT_ENV, "PUBLIC_BASE_URL": url})


def test_env_key_without_a_model_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="LLM_API_KEY needs LLM_MODEL"):
        Settings.from_env({**BASE_ENV, **CONNECT_ENV, "LLM_API_KEY": "x"})


def test_lone_env_model_is_not_a_fallback() -> None:
    settings = Settings.from_env({**BASE_ENV, **CONNECT_ENV, "LLM_MODEL": "gpt-test"})
    assert not settings.has_fallback_llm


@pytest.mark.parametrize("present", ["KEY_ENCRYPTION_KEY", "KEYSTORE_VALKEY_URL"])
def test_half_a_keystore_without_the_page_is_ignored(present: str) -> None:
    settings = Settings.from_env({**REQUIRED_ENV, present: CONNECT_ENV[present]})
    assert not settings.keystore_enabled
    assert settings.has_fallback_llm


def test_lone_bad_encryption_key_is_ignored_while_the_keystore_is_off() -> None:
    assert not Settings.from_env({**REQUIRED_ENV, "KEY_ENCRYPTION_KEY": "not-a-key"}).keystore_enabled


def test_no_llm_source_at_all_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        Settings.from_env(BASE_ENV)


def test_keystore_without_the_page_still_serves_pasted_keys() -> None:
    env = {**BASE_ENV, "KEY_ENCRYPTION_KEY": FERNET_KEY, "KEYSTORE_VALKEY_URL": "redis://keystore"}
    settings = Settings.from_env(env)
    assert settings.keystore_enabled and not settings.connect_enabled


def test_base_url_allowlist_override() -> None:
    settings = Settings.from_env({**REQUIRED_ENV, "LLM_BASE_URL_ALLOWLIST": " LLM.Example , gw.example "})
    assert settings.llm_base_url_allowlist == ("llm.example", "gw.example")


def test_connect_requires_an_edge_secret() -> None:
    env = {**BASE_ENV, **CONNECT_ENV}
    del env["CONNECT_EDGE_SECRET"]
    with pytest.raises(ConfigError, match="CONNECT_EDGE_SECRET"):
        Settings.from_env(env)


def test_connect_refuses_a_short_edge_secret() -> None:
    with pytest.raises(ConfigError, match="at least 32"):
        Settings.from_env({**BASE_ENV, **CONNECT_ENV, "CONNECT_EDGE_SECRET": "e" * 31})


def test_edge_secret_is_not_in_the_repr() -> None:
    settings = Settings.from_env({**BASE_ENV, **CONNECT_ENV})
    assert "e" * 32 not in repr(settings)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Bot.Example.com:443", "https://bot.example.com"),
        ("https://bot.example.com:443/", "https://bot.example.com"),
        ("https://Bot.Example.com:8443", "https://bot.example.com:8443"),
        ("https://[::1]:8443", "https://[::1]:8443"),
    ],
)
def test_public_base_url_is_normalised_to_the_browser_origin(url: str, origin: str) -> None:
    assert Settings.from_env({**REQUIRED_ENV, **CONNECT_ENV, "PUBLIC_BASE_URL": url}).public_base_url == origin


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", False),
        ("false", False),
        ("0", False),
        ("No", False),
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
    ],
)
def test_trust_preferred_username_flag(raw: str, expected: bool) -> None:
    settings = Settings.from_env({**BASE_ENV, **CONNECT_ENV, "CONNECT_TRUST_PREFERRED_USERNAME": raw})
    assert settings.connect_trust_preferred_username is expected


def test_trust_preferred_username_defaults_off() -> None:
    assert not Settings.from_env({**BASE_ENV, **CONNECT_ENV}).connect_trust_preferred_username


def test_trust_preferred_username_rejects_other_words() -> None:
    with pytest.raises(ConfigError, match="CONNECT_TRUST_PREFERRED_USERNAME"):
        Settings.from_env({**BASE_ENV, **CONNECT_ENV, "CONNECT_TRUST_PREFERRED_USERNAME": "maybe"})
