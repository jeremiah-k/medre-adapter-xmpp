# Render the XMPP connection through the offline native setup form

The XMPP registration declares provider-owned native setup prompts, so
`medre config native --transport xmpp` collects the account JID, an optional
password, the optional room nickname, and optional host/port overrides without
contacting any server. The password uses hidden input; leaving it blank keeps
the `MEDRE_ADAPTER__<ID>__PASSWORD` environment override as the credential
source. The port prompt is numeric, host and port overrides must be supplied
together, and the typed config loader validates every answer before the private
output file is written.

The form references an existing account and provisions nothing: creating the
TLS account, choosing `rooms` and `peers`, and granting room join permissions
remain manual steps. Room and peer lists are tuple-valued config fields the
single-value form prompts cannot express, so they stay in the documented manual
configuration path.

Registration stays compatible with older core contracts that lack optional
native-form metadata; manual YAML remains available on those installations.
