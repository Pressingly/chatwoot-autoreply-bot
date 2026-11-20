"""``/connect``: where an allowlisted admin pastes an LLM key for one inbox.

Reachable only through Traefik with mpass-auth ForwardAuth, which strips any
client-supplied ``X-Auth-Request-*`` header before setting the verified one. That
header is read here and nowhere else. A missing header fails closed with 401.

POSTs need the double-submit CSRF token (``__Host-`` cookie plus a hidden field)
and an ``Origin`` (or ``Referer``) equal to ``PUBLIC_BASE_URL``.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import string
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from chatwoot_autoreply_bot.bodies import BodyTooLarge, read_limited_body
from chatwoot_autoreply_bot.connect_form import FormError, parse_connect_form, parse_inbox_id
from chatwoot_autoreply_bot.connect_page import PageView, render_connect_page, render_notice
from chatwoot_autoreply_bot.identity import AdminAllowlist, Identity
from chatwoot_autoreply_bot.keystore import InboxKeyStore
from chatwoot_autoreply_bot.llm import LlmCredentials

log = logging.getLogger(__name__)

IDENTITY_HEADER = "X-Auth-Request-Email"
MPASS_ID_HEADER = "X-Auth-Request-Preferred-Username"
EDGE_SECRET_HEADER = "X-Mpass-Edge-Secret"
CSRF_COOKIE = "__Host-cwbot_csrf"
CSRF_FIELD = "csrf_token"
CSRF_TOKEN_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 8
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

CredentialCheck = Callable[[LlmCredentials], Awaitable[str | None]]
Clock = Callable[[], float]
KEYSTORE_UNAVAILABLE = "The key store is unavailable. Try again shortly."


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ConnectDeps:
    allowlist: AdminAllowlist
    edge_secret: str = field(repr=False)
    public_origin: str
    allowed_hosts: Collection[str]
    check_credentials: CredentialCheck
    keystore_of: Callable[[Request], InboxKeyStore]
    clock: Clock


def connect_router(deps: ConnectDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/connect")
    async def show(request: Request) -> Response:
        return await _guarded(lambda: _show(request, deps))

    @router.post("/connect")
    async def save(request: Request) -> Response:
        return await _guarded(lambda: _save(request, deps))

    @router.post("/connect/{inbox_id}/delete")
    async def delete(request: Request, inbox_id: str) -> Response:
        return await _guarded(lambda: _delete(request, deps, inbox_id))

    return router


async def _guarded(handler: Callable[[], Awaitable[Response]]) -> Response:
    try:
        response = await handler()
    except Refused as refusal:
        response = HTMLResponse(render_notice(refusal.message), status_code=refusal.status)
    response.headers.update(SECURITY_HEADERS)
    return response


async def _show(request: Request, deps: ConnectDeps) -> Response:
    _require_admin(request, deps.allowlist, deps.edge_secret)
    return await _page(request, deps, _csrf_token(request))


async def _save(request: Request, deps: ConnectDeps) -> Response:
    actor = _require_admin(request, deps.allowlist, deps.edge_secret)
    fields = await _verified_form(request, deps.public_origin)
    token = fields[CSRF_FIELD]
    try:
        connect = parse_connect_form(fields, deps.allowed_hosts)
    except FormError as error:
        return await _page(request, deps, token, error=str(error), values=fields, status=422)
    problem = await deps.check_credentials(connect.credentials)
    if problem is not None:
        log.info("llm key check failed mpass_id=%s inbox=%s host=%s", actor, connect.inbox_id, connect.credentials.host)
        return await _page(request, deps, token, error=problem, values=fields, status=422)
    keystore = deps.keystore_of(request)
    await _from_keystore(
        "write", lambda: keystore.save(connect.inbox_id, connect.credentials, set_by=actor, set_at=deps.clock())
    )
    log.info(
        "llm key saved mpass_id=%s inbox=%s host=%s model=%s",
        actor,
        connect.inbox_id,
        connect.credentials.host,
        connect.credentials.model,
    )
    return RedirectResponse("/connect", status_code=303)


async def _delete(request: Request, deps: ConnectDeps, raw_inbox_id: str) -> Response:
    actor = _require_admin(request, deps.allowlist, deps.edge_secret)
    fields = await _verified_form(request, deps.public_origin)
    try:
        inbox_id = parse_inbox_id(raw_inbox_id)
    except FormError as error:
        return await _page(request, deps, fields[CSRF_FIELD], error=str(error), status=422)
    keystore = deps.keystore_of(request)
    removed = await _from_keystore("read", lambda: keystore.connection(inbox_id))
    await _from_keystore("write", lambda: keystore.delete(inbox_id))
    log.info(
        "llm key deleted mpass_id=%s inbox=%s host=%s model=%s",
        actor,
        inbox_id,
        removed.host if removed else "",
        removed.model if removed else "",
    )
    return RedirectResponse("/connect", status_code=303)


def _require_admin(request: Request, allowlist: AdminAllowlist, edge_secret: str) -> str:
    """The admin's actor label (mPass id, never an email) once the edge, identity and allowlist checks pass."""
    if not _came_through_edge(request, edge_secret):
        log.warning("connect denied reason=edge_secret")
        raise Refused(401, "Sign in through mPass to manage auto-reply keys.")
    identity = Identity.from_headers(request.headers.get(IDENTITY_HEADER), request.headers.get(MPASS_ID_HEADER))
    if not identity.email:
        raise Refused(401, "Sign in through mPass to manage auto-reply keys.")
    actor = allowlist.actor_label(identity)
    if not allowlist.permits(identity):
        log.warning("connect denied mpass_id=%s", actor)
        raise Refused(403, "Your account is not allowed to manage auto-reply LLM keys.")
    return actor


def _came_through_edge(request: Request, edge_secret: str) -> bool:
    """Only Traefik's mPass router sets this header; it keeps backend neighbours from forging identity headers."""
    presented = request.headers.get(EDGE_SECRET_HEADER, "")
    return hmac.compare_digest(presented.encode(), edge_secret.encode())


async def _verified_form(request: Request, public_origin: str) -> dict[str, str]:
    """Origin first, then the body, then the double-submit token; any failure is a 403 (or 413/400)."""
    if not _same_origin(request, public_origin):
        raise Refused(403, "This request did not come from the Connect page.")
    fields = await _read_form(request)
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    form_token = fields.get(CSRF_FIELD, "")
    if not cookie_token or not hmac.compare_digest(cookie_token.encode(), form_token.encode()):
        raise Refused(403, "The form expired. Reload the page and try again.")
    return fields


def _same_origin(request: Request, public_origin: str) -> bool:
    origin = request.headers.get("origin")
    if origin is not None:
        return origin.strip().lower() == public_origin
    referer = urlsplit(request.headers.get("referer", ""))
    return bool(referer.netloc) and f"{referer.scheme}://{referer.netloc}".lower() == public_origin


async def _read_form(request: Request) -> dict[str, str]:
    try:
        body = await read_limited_body(request, MAX_FORM_BYTES)
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    except BodyTooLarge as error:
        raise Refused(413, "The form is too large.") from error
    except ValueError as error:
        raise Refused(400, "The form could not be read.") from error
    return {name: values[0] for name, values in parsed.items()}


def _csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE, "")
    return token if _well_formed_token(token) else secrets.token_urlsafe(32)


def _well_formed_token(token: str) -> bool:
    return 32 <= len(token) <= 128 and set(token) <= CSRF_TOKEN_ALPHABET


async def _page(
    request: Request,
    deps: ConnectDeps,
    csrf_token: str,
    *,
    error: str | None = None,
    values: Mapping[str, str] | None = None,
    status: int = 200,
) -> Response:
    keystore = deps.keystore_of(request)
    connections = await _from_keystore("read", keystore.connections)
    inboxes = await _from_keystore("read", keystore.inboxes)
    view = PageView(
        csrf_token=csrf_token,
        allowed_hosts=deps.allowed_hosts,
        connections=connections,
        inboxes=inboxes,
        error=error,
        values=values or {},
    )
    response = HTMLResponse(render_connect_page(view), status_code=status)
    response.set_cookie(CSRF_COOKIE, csrf_token, path="/", secure=True, httponly=True, samesite="strict")
    return response


async def _from_keystore[T](operation: str, call: Callable[[], Awaitable[T]]) -> T:
    """A keystore outage becomes a 503 page with the security headers; only the error class is logged."""
    try:
        return await call()
    except Exception as failure:
        log.error("keystore %s failed error=%s", operation, type(failure).__name__)
        raise Refused(503, KEYSTORE_UNAVAILABLE) from failure
