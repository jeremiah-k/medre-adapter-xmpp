# XMPP direct chat and multi-user room bridging

- Discover the independent XMPP provider through the public MEDRE entry point.
- Validate explicit bare-JID room and peer lists without importing native SDKs.
- Bridge live plain text through verified TLS and XEP-0045 room joins, with
  bounded admission, reconnect ownership and deterministic shutdown.
- Preserve source identity and native references; exclude history, self echoes
  and mutation compatibility bodies from fresh-message admission.
- Reuse MEDRE attribution and reply fallback while reporting only local
  transport handoff evidence.
- Provide SDK-free samples, a pipeline fake, published schemas and offline
  contracts against the pinned Slixmpp SDK.
