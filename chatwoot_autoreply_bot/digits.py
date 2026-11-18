"""Strict parsing of decimal ids and timestamps from untrusted text."""

from __future__ import annotations

import re

MAX_ID_DIGITS = 18


def parse_digits(text: str, max_digits: int = MAX_ID_DIGITS) -> int | None:
    """ASCII digits only (``str.isdigit`` also accepts "²"), bounded so ``int()`` cannot blow up."""
    if not re.fullmatch(rf"[0-9]{{1,{max_digits}}}", text):
        return None
    return int(text)
