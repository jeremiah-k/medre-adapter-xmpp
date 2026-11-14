# Wire supplied XMPP transport facts and occupant identity

Record native facts the connection already negotiates and the rooms already
supply instead of dropping them. Each admitted event now carries the receiving
stream's negotiated TLS state on the canonical transport metadata
(`transport_encrypted`), read from the SDK's socket at receipt time — plaintext
is disabled and STARTTLS/direct TLS are enforced, and a non-TLS observation is
recorded honestly rather than assumed away. The XEP-0421 `occupant-id` supplied
by a room is recorded in the native namespace as the only stable sender anchor
in anonymous MUCs; ambiguous or malformed elements are omitted, never guessed.

A bounded opted-in `public_facts` projection (stanza `message_type` and the
room-issued stanza-ID trust outcome) becomes the single source of xmpp facts
for core's transport-neutral bridge context; the previously dormant
`native_facts` stanza-serialization seam can now also carry xmpp facts
originated here, and the native schema gains `occupant_id`, `public_facts` and
the shared bridge `native_facts` envelope. `metadata_fields` stays true;
reactions, corrections, retractions, moderation, delayed-stanza policy, chat
markers/receipts and XHTML-IM remain fenced out of this change.
