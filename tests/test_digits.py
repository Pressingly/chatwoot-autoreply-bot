from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.digits import parse_digits


@pytest.mark.parametrize(("text", "expected"), [("0", 0), ("42", 42), ("123456789012345678", 123456789012345678)])
def test_parses_ascii_digits(text: str, expected: int) -> None:
    assert parse_digits(text) == expected


@pytest.mark.parametrize("text", ["", "²", "١", "1" * 5000, "1" * 19, "-1", "1.0", " 1", "1\n"])
def test_rejects_everything_else(text: str) -> None:
    assert parse_digits(text) is None


def test_custom_width() -> None:
    assert parse_digits("1" * 12, 12) is not None
    assert parse_digits("1" * 13, 12) is None
