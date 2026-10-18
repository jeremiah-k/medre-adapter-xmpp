# XMPP text adapter contract

## Conversations and identities

MEDRE routes use exact bare JIDs. Configured `rooms` identify XEP-0045 MUC
rooms; configured `peers` identify direct contacts. The lists MUST be disjoint.
The adapter MUST NOT infer a room from a hostname prefix or send to an
unconfigured destination. Configured identifiers MUST match Slixmpp's RFC 7622
prepared representation at native startup; preparation MUST NOT silently alter
a routed recipient.

A room message's source identity is its occupant JID (`room@service/nickname`).
Its conversation is the room's bare JID. A direct message's source identity and
conversation are the sender's bare JID, retaining the same context across
resource changes. Nicknames MUST NOT be promoted to account identities.

The codec MUST derive repeatable canonical IDs from the adapter, conversation,
sender and stanza ID when an ID exists. Missing native IDs MUST remain absent
from native references; independent arrivals without IDs receive distinct
canonical IDs. Sender-provided IDs remain scoped observations, not trusted
server IDs or authorship proofs. The adapter MUST NOT treat an unverified
XEP-0359 stanza ID as globally authoritative.

## Inbound admission

Only configured live plain-text conversations are admitted. The codec MUST
ignore empty bodies, protocol controls without bodies, self echoes, unknown
senders, unsupported message types, oversized text, nested markup in a plain
body, and both XEP-0203 and legacy delayed history. MUC joins MUST request zero
history. This live bridge does not provide offline backlog recovery or MAM
checkpoints; delayed direct messages are deliberately excluded as well.

Correction, retraction, fastening and reaction stanzas MUST NOT be reinterpreted
as fresh text merely because they carry a compatibility body. XEP-0461 reply
references MAY become canonical reply relations without inventing target-event
identity or mutation ownership. Outbound replies use core's textual fallback.

The session MUST bound pending inbound work and report queue overflow and
admission failures through counters and logs. It MUST NOT acknowledge durable
admission, advance an archive cursor, or emit delivery receipts on overflow.
The runtime remains authoritative for admission and persistence.

## Rendering and handoff

Rendering MUST remain separate from delivery. The renderer uses core's shared
source projection, attribution formatter and relation fallback. It MUST preserve
explicit empty origin labels, validate the final text budget after attribution,
and reject oversized output rather than silently truncating it. Slixmpp MUST
serialize XML; body content is plain text even when it contains markup-like
characters. No XHTML, Markdown, source impersonation or puppeting is performed.

Native delivery MUST validate the target adapter, configured destination,
message type, body and budget before writing. A successful handoff means XML
was passed to the local TLS transport during the call. It MUST report
`confirmation_level: local_transport`; local SDK queue admission, server
acceptance, recipient delivery and read state MUST NOT be conflated. The
provider registers no outgoing stanza transformations or stream-management
plugin and uses the SDK's immediate `send_raw` transport seam after SDK stanza
serialization. Any future encryption or stream-management support MUST revisit
this handoff contract rather than bypass its filters.

## Connection lifecycle

Native startup MUST require credentials, verified TLS, and successful joins of
all configured rooms before readiness. Plaintext fallback and certificate
verification overrides are not configuration options. TLS protects the client
connection; the adapter makes no end-to-end encryption claim.

Slixmpp owns DNS SRV discovery, authentication, stanza parsing and MUC protocol
handling. A host override MUST supply its port as well. The session owns bounded
startup time, a bounded ingress queue, and lifetime reconnection with a positive
retry delay. Each replacement connection authenticates and rejoins rooms.
Authentication rejection MUST stop retries. Room self-unavailability MUST
invalidate readiness. Shutdown MUST cancel connection attempts, join handlers,
keepalive schedules, the SDK filter worker, the reconnect task and ingress
worker. Queued work is not durable and MUST NOT survive a session restart.

## Optional protocol features

XEP-0085 defines chat state notifications, including typing. Those events are
ephemeral and MUST NOT be encoded as chat messages or durable delivery lifecycle
events. The adapter ignores bodyless notifications until MEDRE provides a
protocol-neutral ephemeral activity seam with expiry and routing policy.

XEP-0184 delivery receipts and XEP-0333 displayed markers are distinct from
MEDRE's local handoff receipts. Their implementation requires attempt/native-ID
correlation and truthful evidence levels. Neither is advertised here. Presence,
attachments, message mutations, MAM, OMEMO, rich text and puppeting likewise
remain unadvertised. Capability declarations describe implemented adapter
behavior, not everything available in the XMPP ecosystem.
