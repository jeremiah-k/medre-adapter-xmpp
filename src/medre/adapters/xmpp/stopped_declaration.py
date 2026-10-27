"""XMPP declaration: stateless locally, with a server-side credential.

Layout contract read from this distribution: the adapter keeps no local
files; the JID credential lives in configuration (or an environment
override), never in capturable state. Truthful coverage means saying so, not
inventing files to capture. This module follows the seam contract in
``medre.maintenance.stopped_declare``.
"""

from __future__ import annotations

from medre.maintenance.stopped_declare import (
    LocalExclusion,
    ProviderCoverage,
    StoppedStateDeclaration,
    declared,
)

_NOTES = (
    "no local state exists to capture; the account credential lives in "
    "configuration or an environment override and is never selected by a "
    "state snapshot",
    "another host may use the same account concurrently; server resource "
    "rules are not an exclusive installation ownership lock",
)


class XmppStoppedState:
    """Provider-owned declaration of one configured XMPP account."""

    provider = "xmpp"

    def coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage:
        """Declare the honest stateless representation with zero files."""
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(),
            state_class="stateless_local",
            notes=_NOTES,
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        """Report that nothing local can exclude another host's session."""
        return LocalExclusion(
            self.provider,
            note=(
                "no local state and no local ownership primitive exist; "
                "server resource rules do not prove exclusive account use"
            ),
        )


def declaration() -> StoppedStateDeclaration:
    """Return the provider-owned declaration for stopped-state planning."""
    return XmppStoppedState()
