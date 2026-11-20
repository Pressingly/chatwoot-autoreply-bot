"""Who is asking on ``/connect``, and whether they may manage LLM keys.

The identity is the ``X-Auth-Request-Email`` header mpass-auth sets behind Traefik,
which strips any client-supplied copy first. It is normalised like Chatwoot's
``lib/mpass/proxy_identity.rb``: Moneta sends the bare mPass id (cognito:username),
other pools a real email, and Chatwoot synthesises ``<id>@<DEFAULT_EMAIL_DOMAIN>``
from a bare id, so an allowlist entry in any of those forms matches the others.

With email capture on, the email header carries the user's real address and the
mPass id arrives in ``X-Auth-Request-Preferred-Username`` (same edge, same strip),
so an all-digit value there adds the bare id and its synthetic email as aliases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

EMAIL_IDENTITY_LABEL = "email-identity"
BARE_MPASS_ID = re.compile(r"[0-9]+")


def normalise(value: str | None) -> str:
    return (value or "").strip().lower()


def is_email_shaped(value: str) -> bool:
    """indexOf-based like Chatwoot's check: an ``@`` that is not first, then a ``.`` at least one char later."""
    at = value.find("@")
    if at <= 0:
        return False
    return value.find(".", at + 1) > at + 1


def bare_mpass_id(value: str | None) -> str | None:
    """Only an all-digit value is an mPass id; elsewhere the claim can be a display name or a Cognito sub."""
    candidate = normalise(value)
    return candidate if BARE_MPASS_ID.fullmatch(candidate) else None


@dataclass(frozen=True)
class Identity:
    """The verified ``X-Auth-Request-Email`` value, plus the mPass id when the edge sends one separately."""

    email: str
    mpass_id: str | None = None

    @classmethod
    def from_headers(cls, email: str | None, preferred_username: str | None) -> Identity:
        return cls(normalise(email), bare_mpass_id(preferred_username))


@dataclass(frozen=True)
class AdminAllowlist:
    entries: frozenset[str]
    default_email_domain: str = ""

    @classmethod
    def build(cls, entries: Iterable[str], default_email_domain: str | None) -> AdminAllowlist:
        normalised = frozenset(filter(None, map(normalise, entries)))
        return cls(normalised, normalise(default_email_domain))

    def mpass_id(self, identity: str) -> str | None:
        """The bare mPass id behind a normalised identity, or None for a real email address.

        The synthetic suffix is checked before the email shape, so a dotless domain such as ``local`` works too.
        """
        suffix = f"@{self.default_email_domain}"
        if self.default_email_domain and identity.endswith(suffix):
            return identity.removesuffix(suffix) or None
        if is_email_shaped(identity):
            return None
        return identity or None

    def aliases(self, identity: str) -> frozenset[str]:
        mpass_id = self.mpass_id(identity)
        if mpass_id is None:
            return frozenset({identity})
        synthetic = {f"{mpass_id}@{self.default_email_domain}"} if self.default_email_domain else set()
        return frozenset({identity, mpass_id, *synthetic})

    def identity_aliases(self, identity: Identity) -> frozenset[str]:
        extra = self.aliases(identity.mpass_id) if identity.mpass_id else frozenset()
        return self.aliases(identity.email) | extra

    def permits(self, identity: Identity) -> bool:
        return bool(identity.email) and not self.identity_aliases(identity).isdisjoint(self.entries)

    def actor_label(self, identity: Identity) -> str:
        """What logs and the key record show for this person: the mPass id, never an email."""
        return identity.mpass_id or self.mpass_id(identity.email) or EMAIL_IDENTITY_LABEL
