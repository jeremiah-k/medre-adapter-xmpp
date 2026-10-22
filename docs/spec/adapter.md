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
sender and an available message ID. In a MUC, a room-issued XEP-0359
`stanza-id` with `by` equal to the configured bare room JID takes precedence
only after that room advertises `urn:xmpp:sid:0` through XEP-0030 discovery.
Discovery evidence MUST travel with the received stanza through queued admission
and MUST NOT be borrowed from a replacement connection. Verified IDs are scoped
to their assigning room; client IDs are scoped to the full sending JID. Bare-JID
conversation identity MUST NOT collapse independent resource message IDs.
A verified room ID takes precedence
for the native reference, enabling XEP-0461 replies to address that room ID.
The sender-provided `id` remains recorded separately for diagnostics and
canonical deduplication but MUST NOT be registered as a MUC reply native
reference when the room-issued ID is absent. Direct chats continue using the
client `id`. Unknown `by` authorities MUST NOT override the reference. Neither identifier proves
authorship, recipient delivery, or global identity. Independent arrivals
without IDs receive distinct canonical IDs.

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
identity or mutation ownership. Outbound native replies MUST use a reference
bound to the exact destination adapter and conversation. Unknown, contradictory
or incompatible bindings MUST retain readable text fallback. A reply MUST NOT
be represented as a full threaded conversation. Direct message references MUST
prefer an unambiguous XEP-0359 origin ID over a server-rewritten stanza ID.

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
characters. XEP-0461 replies MAY include an XEP-0428 quote fallback, bounded by
the final text budget. The user message MUST NOT be clipped to fit a quote.
Incoming supported fallback ranges MUST use Unicode code-point offsets, preserve
quoted context separately from the answer, and ignore malformed ranges without
removing message text. Fallback-only bodies MUST NOT become fresh messages.
No XHTML, Markdown, source impersonation or puppeting is performed.

Native delivery MUST validate the target adapter, configured destination,
message type, body and budget before writing. A successful handoff means XML
was passed to the local TLS transport during the call. It MUST report
`confirmation_level: local_transport`; local SDK queue admission, server
acceptance, recipient delivery and read state MUST NOT be conflated. The
provider registers no outgoing stanza transformations or stream-management
plugin and uses the SDK's immediate `send_raw` transport seam after SDK stanza
serialization. Any future encryption or stream-management support MUST revisit
this handoff contract rather than bypass its filters.

For MUC sends, the adapter MUST NOT expose its client-generated message ID as
though the room had assigned it. It MAY await a matching own echo for at most
five seconds, with at most 128 pending correlations per connection. Correlation
requires the configured occupant, an unchanged origin ID and body, no delayed
history, and one unambiguous stanza ID assigned by a room advertising XEP-0359.
Direct sends use their own origin-ID namespace. Missing, conflicting, timed-out
or disconnected echo evidence MUST leave the native reference absent; after a
successful transport write, missing correlation alone MUST NOT trigger a failed
handoff or retry. Cancellation and connection shutdown MUST release pending
correlation ownership. Room echoes MUST remain excluded from canonical ingress.
A correlated room echo does not establish recipient delivery or read state.

The configured `max_text_bytes` budget MUST be reported consistently to core
planning and health checks for real and fake adapter instances.

## Connection lifecycle

Native startup MUST require credentials, verified TLS, and successful joins of
all configured rooms before readiness. Plaintext fallback and certificate
verification overrides are not configuration options. TLS protects the client
connection; the adapter makes no end-to-end encryption claim.

Slixmpp owns DNS SRV discovery, authentication, stanza parsing and MUC protocol
handling. A host override MUST supply its port as well. The session owns bounded
startup time, a bounded ingress queue, and lifetime reconnection with a positive
retry delay. Each replacement connection authenticates and rejoins rooms.
Exhaustion of all SASL authentication mechanisms MUST stop retries; a single mechanism failure MAY precede a successful fallback. Unexpected MUC occupant identity is a permanent configuration failure, not a reconnect condition. Room self-unavailability MUST
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
