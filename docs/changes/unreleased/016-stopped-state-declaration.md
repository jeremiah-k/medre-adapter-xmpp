# Declare XMPP stopped state for maintenance capture

Ship a provider-owned stopped-state declaration module and reference it from
the adapter spec through the optional `stopped_state_declaration` symbol. The
declaration reports the adapter's actual shape: configured instances keep no
provider-owned local files, the account credential lives in configuration or
an environment override, and coverage is declared stateless-local with zero
files instead of inventing capturable state. Local exclusion is reported as
unsupported: server resource rules can allow concurrent account sessions and
do not establish exclusive installation ownership. Consumers and the trust-boundary contract live in
`medre.maintenance.stopped_declare` on the core side; a distribution without
a declaration is reported as unsupported there rather than guessed.
