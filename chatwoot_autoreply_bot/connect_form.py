"""Boundary validation for the Connect page form: everything posted is untrusted text."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from chatwoot_autoreply_bot.digits import parse_digits
from chatwoot_autoreply_bot.llm import LlmCredentials

MAX_MODEL_LENGTH = 200
MAX_KEY_LENGTH = 500
MAX_BASE_URL_LENGTH = 500
HTTPS_PORT = 443


class FormError(ValueError):
    """A posted value is not acceptable; the message is safe to show the admin."""


@dataclass(frozen=True)
class ConnectRequest:
    inbox_id: int
    credentials: LlmCredentials


def parse_inbox_id(raw: str) -> int:
    inbox_id = parse_digits(raw.strip())
    if inbox_id is None or inbox_id <= 0:
        raise FormError("Inbox id must be a positive whole number.")
    return inbox_id


def parse_base_url(raw: str, allowed_hosts: Collection[str]) -> str:
    text = raw.strip()
    if not text or len(text) > MAX_BASE_URL_LENGTH or not text.isprintable():
        raise FormError("Base URL is required.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as error:
        raise FormError("Base URL is not a valid URL.") from error
    if parts.scheme != "https":
        raise FormError("Base URL must use https.")
    if "@" in parts.netloc or parts.query or parts.fragment or port not in (None, HTTPS_PORT):
        raise FormError("Base URL must be a plain https URL without credentials, port, query or fragment.")
    host = parts.hostname or ""
    if host not in allowed_hosts:
        raise FormError(f"Base URL host must be one of: {', '.join(sorted(allowed_hosts))}.")
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


def parse_model(raw: str) -> str:
    model = raw.strip()
    if not model or len(model) > MAX_MODEL_LENGTH or not model.isprintable():
        raise FormError(f"Model is required and at most {MAX_MODEL_LENGTH} characters.")
    return model


def parse_api_key(raw: str) -> str:
    api_key = raw.strip()
    if not api_key or len(api_key) > MAX_KEY_LENGTH or not api_key.isprintable() or " " in api_key:
        raise FormError(f"API key is required, at most {MAX_KEY_LENGTH} characters, without spaces.")
    return api_key


def parse_connect_form(fields: Mapping[str, str], allowed_hosts: Collection[str]) -> ConnectRequest:
    inbox_id = parse_inbox_id(fields.get("inbox_id", ""))
    credentials = LlmCredentials(
        base_url=parse_base_url(fields.get("base_url", ""), allowed_hosts),
        model=parse_model(fields.get("model", "")),
        api_key=parse_api_key(fields.get("api_key", "")),
    )
    return ConnectRequest(inbox_id, credentials)
