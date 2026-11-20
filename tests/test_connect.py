from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from chatwoot_autoreply_bot import app as app_module
from chatwoot_autoreply_bot.app import Services, create_app
from chatwoot_autoreply_bot.config import Settings
from chatwoot_autoreply_bot.connect import CSRF_COOKIE, SECURITY_HEADERS
from chatwoot_autoreply_bot.events import InboxRef
from chatwoot_autoreply_bot.keystore import KEYS_HASH, InboxKeyStore, KeyCipher
from chatwoot_autoreply_bot.llm import LlmCredentials
from chatwoot_autoreply_bot.store import ValkeyStore
from tests.helpers import FERNET_KEY
from tests.test_app import SETTINGS, SpyScheduler

ORIGIN = "https://chatwoot-autoreply.example.com"
ADMIN = "1234"
DOMAIN = "foss.example"
API_KEY = "sk-live-abcdefghijklmnop"
CONNECT_SETTINGS = replace(
    SETTINGS,
    connect_admin_ids=(ADMIN,),
    key_encryption_key=FERNET_KEY,
    keystore_valkey_url="redis://unused-keystore",
    public_base_url=ORIGIN,
    default_email_domain=DOMAIN,
)
VALID_FORM = {
    "inbox_id": "3",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-test",
    "api_key": API_KEY,
}


class KeyCheck:
    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.checked: list[LlmCredentials] = []

    async def __call__(self, credentials: LlmCredentials) -> str | None:
        self.checked.append(credentials)
        return self.problem


class Page:
    def __init__(self, client: httpx.AsyncClient, keystore: InboxKeyStore, check: KeyCheck) -> None:
        self.client = client
        self.keystore = keystore
        self.check = check

    async def get(self, identity: str | None = ADMIN) -> httpx.Response:
        return await self.client.get("/connect", headers=_identity(identity))

    async def token(self, identity: str = ADMIN) -> str:
        response = await self.get(identity)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        return match.group(1)

    async def post(
        self,
        path: str = "/connect",
        fields: dict[str, str] | None = None,
        *,
        identity: str | None = ADMIN,
        origin: str | None = ORIGIN,
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        form = dict(VALID_FORM if fields is None else fields)
        form["csrf_token"] = token if token is not None else await self.token(identity or ADMIN)
        headers = {**_identity(identity), **({"Origin": origin} if origin is not None else {}), **(extra_headers or {})}
        return await self.client.post(path, data=form, headers=headers)


def _identity(identity: str | None) -> dict[str, str]:
    return {} if identity is None else {"X-Auth-Request-Email": identity}


@pytest.fixture
def keystore(valkey: FakeAsyncRedis) -> InboxKeyStore:
    return InboxKeyStore(valkey, KeyCipher(FERNET_KEY))


@pytest.fixture
def check() -> KeyCheck:
    return KeyCheck()


async def page_for(
    settings: Settings, store: ValkeyStore, keystore: InboxKeyStore, check: KeyCheck
) -> AsyncIterator[Page]:
    services = Services(store, SpyScheduler(), keystore=keystore)  # type: ignore[arg-type]
    app = create_app(settings, lambda _: services, clock=lambda: 1_800_000_000, credential_check=check)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield Page(client, keystore, check)


@pytest.fixture
async def page(store: ValkeyStore, keystore: InboxKeyStore, check: KeyCheck) -> AsyncIterator[Page]:
    async for value in page_for(CONNECT_SETTINGS, store, keystore, check):
        yield value


@pytest.mark.parametrize(("method", "path"), [("GET", "/connect"), ("POST", "/connect"), ("POST", "/connect/3/delete")])
async def test_routes_are_absent_when_the_allowlist_is_empty(
    store: ValkeyStore, keystore: InboxKeyStore, check: KeyCheck, method: str, path: str
) -> None:
    async for page in page_for(SETTINGS, store, keystore, check):
        response = await page.client.request(method, path, headers=_identity(ADMIN))
        assert response.status_code == 404


async def test_missing_identity_is_401(page: Page) -> None:
    response = await page.get(identity=None)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


async def test_blank_identity_is_401(page: Page) -> None:
    assert (await page.get(identity="   ")).status_code == 401


async def test_post_without_identity_is_401(page: Page) -> None:
    response = await page.post(identity=None, token="x" * 43)
    assert response.status_code == 401
    assert await page.keystore.connections() == []


async def test_unlisted_identity_is_403_and_logged_without_email(page: Page, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    response = await page.get(identity="intruder@corp.example")
    assert response.status_code == 403
    assert "not allowed" in response.text
    assert "intruder" not in caplog.text
    assert (await page.get(identity="5678")).status_code == 403
    assert "mpass_id=5678" in caplog.text


@pytest.mark.parametrize("identity", [ADMIN, f"{ADMIN}@{DOMAIN}", f" {ADMIN}@{DOMAIN.upper()} "])
async def test_admin_in_any_form_sees_the_page(page: Page, identity: str) -> None:
    response = await page.get(identity=identity)
    assert response.status_code == 200
    assert "Auto-reply LLM keys" in response.text


async def test_page_sets_a_host_prefixed_strict_csrf_cookie_and_security_headers(page: Page) -> None:
    response = await page.get()
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{CSRF_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_csrf_token_is_stable_across_page_loads(page: Page) -> None:
    assert await page.token() == await page.token()


async def test_malformed_csrf_cookie_is_replaced(page: Page) -> None:
    page.client.cookies.set(CSRF_COOKIE, "bad token", domain="chatwoot-autoreply.example.com")
    assert await page.token() != "bad token"


async def test_save_validates_encrypts_and_redirects(page: Page, valkey: FakeAsyncRedis, caplog) -> None:
    caplog.set_level(logging.INFO)
    response = await page.post()
    assert response.status_code == 303
    assert response.headers["location"] == "/connect"
    assert page.check.checked == [LlmCredentials("https://api.openai.com/v1", "gpt-test", API_KEY)]
    assert await page.keystore.credentials(3) == page.check.checked[0]
    assert API_KEY.encode() not in (await valkey.hget(KEYS_HASH, "3") or b"")
    assert "llm key saved mpass_id=1234 inbox=3 host=api.openai.com model=gpt-test" in caplog.text
    assert API_KEY not in caplog.text


async def test_page_lists_connections_without_the_key(page: Page) -> None:
    await page.keystore.remember_inbox(InboxRef(3, 1, "Website"))
    await page.keystore.remember_inbox(InboxRef(4, 1, "WhatsApp"))
    await page.post()
    html = (await page.get()).text
    assert "Website" in html and "WhatsApp" in html
    assert "api.openai.com" in html and "gpt-test" in html
    assert "ending mnop" in html
    assert "2027-01-15 08:00 UTC" in html
    assert API_KEY not in html
    assert 'action="/connect/3/delete"' in html
    assert 'action="/connect/4/delete"' not in html


async def test_inbox_names_are_escaped(page: Page) -> None:
    await page.keystore.remember_inbox(InboxRef(3, 1, "<script>alert(1)</script>"))
    html = (await page.get()).text
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


async def test_email_admin_is_recorded_as_a_placeholder(page: Page, store, keystore, check) -> None:
    settings = replace(CONNECT_SETTINGS, connect_admin_ids=("admin@corp.example",))
    async for email_page in page_for(settings, store, keystore, check):
        response = await email_page.post(identity="admin@corp.example")
        assert response.status_code == 303
    assert (await keystore.connections())[0].set_by == "email-identity"


async def test_missing_csrf_token_is_403(page: Page) -> None:
    await page.get()
    response = await page.client.post("/connect", data=VALID_FORM, headers={**_identity(ADMIN), "Origin": ORIGIN})
    assert response.status_code == 403
    assert page.check.checked == []


async def test_mismatched_csrf_token_is_403(page: Page) -> None:
    await page.get()
    response = await page.post(token="y" * 43)
    assert response.status_code == 403
    assert await page.keystore.connections() == []


async def test_csrf_token_without_cookie_is_403(page: Page) -> None:
    token = await page.token()
    page.client.cookies.clear()
    assert (await page.post(token=token)).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://chatwoot-autoreply.example.com"])
async def test_foreign_origin_is_403(page: Page, origin: str) -> None:
    response = await page.post(origin=origin)
    assert response.status_code == 403
    assert page.check.checked == []


async def test_missing_origin_falls_back_to_referer(page: Page) -> None:
    ok = await page.post(origin=None, extra_headers={"Referer": f"{ORIGIN}/connect"})
    assert ok.status_code == 303
    bad = await page.post(origin=None, extra_headers={"Referer": "https://evil.example/connect"})
    assert bad.status_code == 403


async def test_missing_origin_and_referer_is_403(page: Page) -> None:
    assert (await page.post(origin=None)).status_code == 403


async def test_invalid_form_is_422_and_never_refills_the_key(page: Page) -> None:
    response = await page.post(fields={**VALID_FORM, "base_url": "https://evil.example/v1"})
    assert response.status_code == 422
    assert "Base URL host must be one of" in response.text
    assert "https://evil.example/v1" in response.text
    assert API_KEY not in response.text
    assert page.check.checked == []


async def test_rejected_key_is_422_with_the_checker_message(page: Page) -> None:
    page.check.problem = "The provider rejected this API key."
    response = await page.post()
    assert response.status_code == 422
    assert "The provider rejected this API key." in response.text
    assert API_KEY not in response.text
    assert await page.keystore.connections() == []


async def test_oversized_form_is_413(page: Page) -> None:
    response = await page.post(fields={**VALID_FORM, "model": "m" * 20_000})
    assert response.status_code == 413


async def test_unreadable_form_is_400(page: Page) -> None:
    await page.get()
    headers = {**_identity(ADMIN), "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
    response = await page.client.post("/connect", content=b"a=\xff\xfe", headers=headers)
    assert response.status_code == 400


async def test_too_many_fields_is_400(page: Page) -> None:
    await page.get()
    headers = {**_identity(ADMIN), "Origin": ORIGIN}
    response = await page.client.post("/connect", data={f"f{i}": "x" for i in range(20)}, headers=headers)
    assert response.status_code == 400


async def test_delete_removes_the_key_and_logs_it(page: Page, caplog: pytest.LogCaptureFixture) -> None:
    await page.post()
    caplog.set_level(logging.INFO)
    response = await page.post("/connect/3/delete", fields={})
    assert response.status_code == 303
    assert await page.keystore.credentials(3) is None
    assert "llm key deleted mpass_id=1234 inbox=3 host=api.openai.com model=gpt-test" in caplog.text


async def test_delete_of_an_unknown_inbox_is_harmless(page: Page) -> None:
    assert (await page.post("/connect/9/delete", fields={})).status_code == 303


async def test_delete_needs_csrf_and_origin(page: Page) -> None:
    await page.post()
    assert (await page.post("/connect/3/delete", fields={}, origin="https://evil.example")).status_code == 403
    assert (await page.post("/connect/3/delete", fields={}, token="z" * 43)).status_code == 403
    assert await page.keystore.credentials(3) is not None


async def test_delete_with_a_bad_inbox_id_is_422(page: Page) -> None:
    assert (await page.post("/connect/0/delete", fields={})).status_code == 422


async def test_keystore_outage_is_503(store: ValkeyStore, valkey: FakeAsyncRedis, check: KeyCheck) -> None:
    class DownKeystore(InboxKeyStore):
        async def connections(self):
            raise ConnectionError("down")

    async for page in page_for(CONNECT_SETTINGS, store, DownKeystore(valkey, KeyCipher(FERNET_KEY)), check):
        response = await page.get()
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"


async def test_default_key_check_probes_once_with_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch, store: ValkeyStore, keystore: InboxKeyStore
) -> None:
    seen: list[tuple[LlmCredentials, object]] = []

    async def probe(credentials: LlmCredentials, limits) -> str:
        seen.append((credentials, limits))
        return "The provider rejected this API key."

    monkeypatch.setattr(app_module, "check_credentials", probe)
    async for page in page_for(CONNECT_SETTINGS, store, keystore, None):  # type: ignore[arg-type]
        assert (await page.post()).status_code == 422
    (credentials, limits), *_ = seen
    assert credentials.api_key == API_KEY
    assert limits.max_retries == 0
    assert limits.timeout == 15
