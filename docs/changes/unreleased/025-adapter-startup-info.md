# Expose startup-info facts at the ready transition

The adapter now offers a pure-read `startup_info()` provider that core
discovers duck-typed and renders at the adapter's ready transition. It
reports the configured account JID's localpart and domain, the optional
configured host/port connection override, and the installed slixmpp
distribution version read from package metadata (never a source
literal). Server software is knowable only through a disco network
probe, so it is never reported; an unconfigured JID or a missing
host/port override (DNS SRV mode) omits the corresponding facts rather
than probing them. The hook performs no stanza, discovery, or DNS I/O,
mutates no session state, and never gates, blocks, or delays readiness
— failure or timeout simply yields no facts. The SDK-free fake mirrors
the same duck-typed seam.
