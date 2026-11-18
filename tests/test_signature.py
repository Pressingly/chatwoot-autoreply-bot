from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.signature import sign, verify

SECRET = "bot-secret"
NOW = 1_800_000_000
BODY = b'{"event":"message_created"}'


def check(timestamp: str | None, signature: str | None, body: bytes = BODY, now: float = NOW) -> bool:
    return verify(SECRET, timestamp, signature, body, now=now, max_skew_seconds=300)


def test_sign_matches_chatwoot_format() -> None:
    signature = sign(SECRET, "1", b"{}")
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_valid_signature_passes() -> None:
    assert check(str(NOW), sign(SECRET, str(NOW), BODY))


def test_tampered_body_fails() -> None:
    assert not check(str(NOW), sign(SECRET, str(NOW), BODY), body=BODY + b" ")


def test_wrong_secret_fails() -> None:
    assert not check(str(NOW), sign("other", str(NOW), BODY))


def test_signature_bound_to_timestamp() -> None:
    assert not check(str(NOW + 1), sign(SECRET, str(NOW), BODY))


@pytest.mark.parametrize("offset", [301, -301, 10_000])
def test_stale_or_future_timestamp_fails(offset: int) -> None:
    timestamp = str(NOW + offset)
    assert not check(timestamp, sign(SECRET, timestamp, BODY))


@pytest.mark.parametrize("offset", [300, -300, 0])
def test_timestamp_inside_window_passes(offset: int) -> None:
    timestamp = str(NOW + offset)
    assert check(timestamp, sign(SECRET, timestamp, BODY))


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [(None, "sha256=00"), (str(NOW), None), ("", "sha256=00"), (str(NOW), "")],
)
def test_missing_headers_fail(timestamp: str | None, signature: str | None) -> None:
    assert not check(timestamp, signature)


@pytest.mark.parametrize("timestamp", ["abc", "-5", "1.5", " 1800000000"])
def test_malformed_timestamp_fails(timestamp: str) -> None:
    assert not check(timestamp, sign(SECRET, timestamp, BODY))


def test_bare_hex_without_prefix_fails() -> None:
    assert not check(str(NOW), sign(SECRET, str(NOW), BODY).removeprefix("sha256="))


def test_non_ascii_signature_fails_without_raising() -> None:
    assert not check(str(NOW), "sha256=é")
