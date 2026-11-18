"""Chatwoot webhook signature check.

Chatwoot signs ``"<timestamp>.<raw body>"`` with HMAC-SHA256 under the Agent Bot's
secret and sends ``X-Chatwoot-Signature: sha256=<hex>`` with ``X-Chatwoot-Timestamp``.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def _timestamp_fresh(timestamp: str, now: float, max_skew_seconds: int) -> bool:
    if not timestamp.isdigit():
        return False
    return abs(now - int(timestamp)) <= max_skew_seconds


def verify(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    now: float,
    max_skew_seconds: int,
) -> bool:
    if not timestamp or not signature:
        return False
    if not _timestamp_fresh(timestamp, now, max_skew_seconds):
        return False
    expected = sign(secret, timestamp, body).encode()
    return hmac.compare_digest(expected, signature.encode("utf-8", errors="replace"))
