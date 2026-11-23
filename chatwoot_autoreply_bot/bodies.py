"""Bounded request body reading, shared by the webhook and the Connect form."""

from __future__ import annotations

from fastapi import Request

from chatwoot_autoreply_bot.digits import parse_digits


class BodyTooLarge(Exception):
    """The request body exceeds the configured limit."""


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Read the body before it is authenticated, refusing anything over ``max_bytes`` without buffering it."""
    declared = parse_digits(request.headers.get("content-length", ""))
    if declared is not None and declared > max_bytes:
        raise BodyTooLarge
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)
