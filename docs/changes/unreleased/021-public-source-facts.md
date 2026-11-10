# Bounded native source facts over XMPP stanza extensions

The existing v1 bridge-context extension now carries an optional JSON-encoded
source-owned namespace and bounded scalar fact map. The extension validates
size and data type, rejects ambiguous repeated fact elements, and preserves
ordinary visible stanza bodies if optional remote claims are malformed.

Reject public claims that cannot be encoded as UTF-8 on both outbound and
inbound paths without discarding the surrounding native message.

Preserve configured origin labels when supplied attribution leaves the label
unspecified. Explicit route labels and snapshot labels, including empty labels,
retain precedence over the configured source label.
