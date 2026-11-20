"""Who is asking on ``/connect``, and whether they may manage LLM keys.

The identity is the ``X-Auth-Request-Email`` header mpass-auth sets behind Traefik,
which strips any client-supplied copy first. It is normalised like Chatwoot's
``lib/mpass/proxy_identity.rb``: Moneta sends the bare mPass id (cognito:username),
other pools a real email, and Chatwoot synthesises ``<id>@<DEFAULT_EMAIL_DOMAIN>``
from a bare id, so an allowlist entry in any of those forms matches the others.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

EMAIL_IDENTITY_LABEL = "email-identity"


def normalise(value: str | None) -> str:
    return (value or "").strip().lower()


def is_email_shaped(value: str) -> bool:
    """indexOf-based like Chatwoot's check: an ``@`` that is not first, then a ``.`` at least one char later."""
    at = value.find("@")
    if at <= 0:
        return False
    return value.find(".", at + 1) > at + 1


@dataclass(frozen=True)
class AdminAllowlist:
    entries: frozenset[str]
    default_email_domain: str = ""

    @classmethod
    def build(cls, entries: Iterable[str], default_email_domain: str | None) -> AdminAllowlist:
        normalised = frozenset(filter(None, map(normalise, entries)))
        return cls(normalised, normalise(default_email_domain))

    def mpass_id(self, identity: str) -> str | None:
        """The bare mPass id behind a normalised identity, or None for a real email address."""
        if not is_email_shaped(identity):
            return identity or None
        suffix = f"@{self.default_email_domain}"
        if self.default_email_domain and identity.endswith(suffix):
            return identity.removesuffix(suffix) or None
        return None

    def aliases(self, identity: str) -> frozenset[str]:
        mpass_id = self.mpass_id(identity)
        if mpass_id is None:
            return frozenset({identity})
        synthetic = {f"{mpass_id}@{self.default_email_domain}"} if self.default_email_domain else set()
        return frozenset({identity, mpass_id, *synthetic})

    def permits(self, identity: str) -> bool:
        return bool(identity) and not self.aliases(identity).isdisjoint(self.entries)

    def actor_label(self, identity: str) -> str:
        """What logs and the key record show for this person: the mPass id, never an email."""
        return self.mpass_id(identity) or EMAIL_IDENTITY_LABEL
