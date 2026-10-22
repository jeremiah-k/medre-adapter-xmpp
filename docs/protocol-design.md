# Protocol choices and extension seams

The provider uses [Slixmpp 1.17.0](https://pypi.org/project/slixmpp/1.17.0/), an
async Python XMPP implementation, so MEDRE does not implement DNS discovery,
SASL, TLS negotiation, XML streams or MUC from scratch. The SDK is pinned in the
provider metadata; MEDRE core remains independent of that dependency.

| Contract                                                                                                                                         | Decision                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| [RFC 6121 messaging](https://www.rfc-editor.org/rfc/rfc6121)                                                                                     | Direct `chat`/`normal` bodies use a configured peer's bare JID.                                                    |
| [RFC 7622 JIDs](https://www.rfc-editor.org/rfc/rfc7622)                                                                                          | Configuration retains exact addresses; Slixmpp prepares them before native startup and rejects changed identities. |
| [XEP-0045 MUC](https://xmpp.org/extensions/xep-0045.html)                                                                                        | Join configured rooms, request no history, retain occupant JIDs, suppress own echoes.                              |
| [XEP-0199 ping](https://xmpp.org/extensions/xep-0199.html)                                                                                       | Enable SDK keepalive after readiness; close and rejoin after a dead connection.                                    |
| [XEP-0203 delay](https://xmpp.org/extensions/xep-0203.html)                                                                                      | Exclude delayed history from this live-only bridge.                                                                |
| [XEP-0359 IDs](https://xmpp.org/extensions/xep-0359.html)                                                                                        | Avoid trusting arbitrary stanza-ID assertions without feature and assigning-entity verification.                   |
| [XEP-0393 styling](https://xmpp.org/extensions/xep-0393.html) | Preserve literal block/span directives as canonical presentation; use explicit unstyled controls when generated styles cannot preserve their intended scope. |
| [XEP-0461 replies](https://xmpp.org/extensions/xep-0461.html)                                                                                    | Retain scoped native replies with XEP-0428 quote fallback; degrade missing targets to text.                         |
| [XEP-0085 chat states](https://xmpp.org/extensions/xep-0085.html)                                                                                | Keep bodyless activity out of durable message history; add forwarding only through an ephemeral core seam.         |
| [XEP-0184 delivery receipts](https://xmpp.org/extensions/xep-0184.html), [XEP-0333 displayed markers](https://xmpp.org/extensions/xep-0333.html) | Keep protocol observations separate from local transport handoff; no receipt capability is claimed.                |

This is a text bridge, not a complete XMPP client. The current canonical envelope
already represents source identity, native references, reply relations and
transport-native metadata without schema changes. Typing needs a bounded-lived
activity path; it does not justify putting transient state in stored message
payloads. Per-peer feature discovery is required before adding optional native
extensions. A platform-wide flag cannot prove that a particular peer supports
an extension.

Puppeting is a separate authentication and ownership concern. Attribution
labels remain display text while the configured bot remains the authenticated
principal. Native edit/delete support would require core's persisted ownership
and target-binding facts plus the appropriate XMPP extension, not merely a
matching nickname or a claimed sender field.

The native SDK binding is isolated in `connection.py`. The session owns its
lifecycle and bounded ingress; the codec translates received elements; the
renderer owns presentation. Tests replace the connection at that existing seam,
while SDK contracts use real Slixmpp stanza classes and verify serialization,
MUC history parameters, TLS policy and worker cleanup without an account.

## Native validation

The opt-in two-account test also ran against the official
[Prosody Docker image](https://prosody.im/doc/docker), image digest
`sha256:0d4efc7c11018dfd863c9a25495efe706027de357a58a18ef02f8c484fb264ed`
(Prosody 13.0 nightly build 100, server commit `cafd74b9c5ea`), with Slixmpp
1.17.0 on Python 3.14. The disposable server exposed only loopback ports. A
locally trusted test certificate retained TLS verification and hostname checks.
The proof received a Unicode MUC body and its occupant identity through the real
adapter, then received the adapter's direct outbound text at a second account.
The adapter's handoff evidence remained `local_transport` despite the test's
independent recipient observation. This establishes local interoperability;
federation, production server policy and sustained recovery need separate runs.
