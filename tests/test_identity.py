from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.identity import (
    EMAIL_IDENTITY_LABEL,
    AdminAllowlist,
    Identity,
    bare_mpass_id,
    is_email_shaped,
    normalise,
)

DOMAIN = "foss.example"


def allowlist(*entries: str, domain: str | None = DOMAIN) -> AdminAllowlist:
    return AdminAllowlist.build(entries, domain)


def who(email: str, preferred_username: str | None = None) -> Identity:
    return Identity.from_headers(email, preferred_username)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, ""), ("", ""), ("  1234  ", "1234"), (" Alice@Example.COM ", "alice@example.com")],
)
def test_normalise_strips_and_lowercases(raw: str | None, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    ("value", "shaped"),
    [
        ("a@b.co", True),
        ("1234@foss.example", True),
        ("1234", False),
        ("@b.co", False),
        ("a@b", False),
        ("a@.co", False),
        ("a.b@c", False),
        ("a@b.", True),
    ],
)
def test_email_shape_matches_chatwoot(value: str, shaped: bool) -> None:
    assert is_email_shaped(value) is shaped


@pytest.mark.parametrize(
    ("entry", "header"),
    [
        ("1234", "1234"),
        ("1234", f"1234@{DOMAIN}"),
        (f"1234@{DOMAIN}", "1234"),
        (f"1234@{DOMAIN}", f"1234@{DOMAIN}"),
        ("admin@corp.example", "admin@corp.example"),
        (" ADMIN@Corp.Example ", "admin@corp.example"),
        (f" 1234@{DOMAIN.upper()} ", "1234"),
    ],
)
def test_every_form_of_an_entry_matches(entry: str, header: str) -> None:
    assert allowlist(entry).permits(who(header))


@pytest.mark.parametrize(
    "header",
    ["", "12345", "123", "1234@other.example", "1234@foss.example.evil", "admin@corp.example"],
)
def test_other_identities_are_refused(header: str) -> None:
    assert not allowlist("1234", f"9999@{DOMAIN}").permits(who(header))


@pytest.mark.parametrize(("entry", "header"), [("1234", "1234@local"), ("1234@local", "1234"), ("1234", "1234")])
def test_dotless_synthetic_domain_still_matches(entry: str, header: str) -> None:
    assert allowlist(entry, domain="local").permits(who(header))


def test_without_a_domain_only_exact_matches_count() -> None:
    entries = allowlist("1234", domain=None)
    assert entries.permits(who("1234"))
    assert not entries.permits(who(f"1234@{DOMAIN}"))


def test_blank_entries_are_ignored() -> None:
    assert allowlist(" ", "").entries == frozenset()
    assert not allowlist(" ").permits(who(""))


@pytest.mark.parametrize(
    ("identity", "label"),
    [("1234", "1234"), (f"1234@{DOMAIN}", "1234"), ("admin@corp.example", EMAIL_IDENTITY_LABEL)],
)
def test_actor_label_never_reveals_an_email(identity: str, label: str) -> None:
    assert allowlist("1234").actor_label(who(identity)) == label


def test_empty_identity_has_no_mpass_id() -> None:
    assert allowlist("1234").mpass_id("") is None


REAL_EMAIL = "admin@realdomain.example"


@pytest.mark.parametrize("entry", ["1234", f"1234@{DOMAIN}", REAL_EMAIL])
def test_capture_on_identity_matches_a_bare_id_synthetic_or_real_email_entry(entry: str) -> None:
    assert allowlist(entry).permits(who(REAL_EMAIL, " 1234 "))


@pytest.mark.parametrize(
    "preferred_username",
    ["1234@foss.example", "Admin", "3f2b1c9e-8d7a-4e6f-9b0a-1c2d3e4f5a6b", "12 34", "１２３４", "", None],
)
def test_preferred_username_that_is_not_a_bare_id_is_ignored(preferred_username: str | None) -> None:
    identity = who(REAL_EMAIL, preferred_username)
    assert identity.mpass_id is None
    assert not allowlist("1234", "admin", "3f2b1c9e-8d7a-4e6f-9b0a-1c2d3e4f5a6b").permits(identity)
    assert allowlist("1234").actor_label(identity) == EMAIL_IDENTITY_LABEL


def test_preferred_username_alone_is_not_an_identity() -> None:
    assert not allowlist("1234").permits(who("", "1234"))


def test_actor_label_prefers_the_mpass_id_over_the_email() -> None:
    assert allowlist(REAL_EMAIL).actor_label(who(REAL_EMAIL, "1234")) == "1234"


def test_bare_mpass_id_is_ascii_digits_only() -> None:
    assert bare_mpass_id(" 0042 ") == "0042"
    assert bare_mpass_id("4²") is None


@pytest.mark.parametrize("own_identity", ["5555", f"5555@{DOMAIN}"])
def test_second_id_beside_a_bare_or_synthetic_identity_is_ignored(own_identity: str) -> None:
    identity = who(own_identity, "1234")
    entries = allowlist("1234")
    assert not entries.permits(identity)
    assert entries.actor_label(identity) == "5555"
