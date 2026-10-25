# Preserve original sender and origin claims in XMPP

Embed the shared bounded relay context in a native XML stanza extension. Source
names, origin labels, verification descriptions and scoped references survive
independently of visible prefixes. Retain normalized inbound context as remote
claims without changing native sender identity or reply authority. Operators may
disable embedding; malformed and unrepresentable optional context leaves text
usable. XMPP now declares bounded metadata-field support.
