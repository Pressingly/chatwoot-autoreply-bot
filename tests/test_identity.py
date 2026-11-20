from __future__ import annotations

import pytest

from chatwoot_autoreply_bot.identity import EMAIL_IDENTITY_LABEL, AdminAllowlist, is_email_shaped, normalise

DOMAIN = "foss.example"


def allowlist(*entries: str, domain: str | None = DOMAIN) -> AdminAllowlist:
    return AdminAllowlist.build(entries, domain)


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
    assert allowlist(entry).permits(normalise(header))


@pytest.mark.parametrize(
    "header",
    ["", "12345", "123", "1234@other.example", "1234@foss.example.evil", "admin@corp.example"],
)
def test_other_identities_are_refused(header: str) -> None:
    assert not allowlist("1234", f"9999@{DOMAIN}").permits(header)


@pytest.mark.parametrize(("entry", "header"), [("1234", "1234@local"), ("1234@local", "1234"), ("1234", "1234")])
def test_dotless_synthetic_domain_still_matches(entry: str, header: str) -> None:
    assert allowlist(entry, domain="local").permits(header)


def test_without_a_domain_only_exact_matches_count() -> None:
    entries = allowlist("1234", domain=None)
    assert entries.permits("1234")
    assert not entries.permits(f"1234@{DOMAIN}")


def test_blank_entries_are_ignored() -> None:
    assert allowlist(" ", "").entries == frozenset()
    assert not allowlist(" ").permits("")


@pytest.mark.parametrize(
    ("identity", "label"),
    [("1234", "1234"), (f"1234@{DOMAIN}", "1234"), ("admin@corp.example", EMAIL_IDENTITY_LABEL)],
)
def test_actor_label_never_reveals_an_email(identity: str, label: str) -> None:
    assert allowlist("1234").actor_label(identity) == label


def test_empty_identity_has_no_mpass_id() -> None:
    assert allowlist("1234").mpass_id("") is None
