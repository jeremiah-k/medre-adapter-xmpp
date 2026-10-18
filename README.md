# MEDRE XMPP adapter

Bridge live text between configured XMPP contacts, multi-user rooms and other
[MEDRE](https://github.com/jeremiah-k/medre) transports. The adapter uses Slixmpp
for XMPP negotiation, authentication, TLS and MUC; MEDRE handles routing,
persistence, policy and delivery planning.

## Install

For a MEDRE installation managed by uv tools, install the provider explicitly:

```bash
medre adapters install 'medre-adapter-xmpp @ git+https://github.com/jeremiah-k/medre-adapter-xmpp.git@main'
medre adapters check
```

Use a full merged commit instead of `main` for repeatable deployments. No sibling
clones are required. A custom source catalog can provide an `xmpp` alias:

```yaml
adapters:
  xmpp: medre-adapter-xmpp @ git+https://github.com/jeremiah-k/medre-adapter-xmpp.git@main
```

Use `medre adapters install xmpp --sources /path/to/sources.yaml` with that catalog.
The catalog chooses package sources; runtime YAML contains connection settings.

## Configure

Create an account on an XMPP server with a publicly trusted TLS certificate and
join permissions for the selected rooms. Protect the runtime config as private.

```yaml
adapters:
  xmpp:
    chat:
      adapter_kind: real
      jid: bridge@example.org/medre
      nickname: medre
      rooms: [bridge@conference.example.org]
      peers: [friend@example.org]
      origin_label: XMPP
      # password is supplied through the environment, not version control.
```

Set `MEDRE_ADAPTER__CHAT__PASSWORD` in the process environment through the secret
management mechanism used for the deployment. Both the account's domain and
configured conversations use prepared JIDs. Host and port overrides are optional
and must appear together; omit both to use DNS SRV discovery. The connection
requires verified TLS and offers no plaintext or certificate-check bypass.
Transport encryption is not end-to-end encryption.

Use `medre config bridge` to connect the instance to another configured adapter.
Use the room's bare JID as its context, or a configured peer's bare JID for a
direct conversation. `medre routes plan` previews the routes offline. The
adapter rejects destinations absent from `rooms` and `peers` and does not infer
rooms from server names. The source side of room messages identifies the actual
occupant (`room@service/nick`); direct messages use the contact's bare JID.

`medre config sample` includes SDK-free fake XMPP values after installation.
Fake smoke evidence exercises routing and rendering; it proves no server login
or native transport delivery.

## Behavior

- Relay plain text with MEDRE's shared attribution formatter. `relay_prefix`
  defaults to `[{origin_label}] {sender}: `; an empty string suppresses it.
- Join every configured MUC room before readiness, request zero history, and
  rejoin after disconnect. Authentication rejection requires operator action.
- Exclude self echoes, delayed history, bodyless typing/receipt notifications,
  and unsupported edit/retract/reaction compatibility bodies.
- Retain inbound reply references and use textual fallback for outbound replies.
- Bound pending inbound work and reject oversized output without silent clipping.
- Report local TLS transport writes separately from recipient delivery/read state.

This live bridge does not recover offline messages or room archives. Rich text,
attachments, presence, typing forwarding, read receipts, native mutations,
OMEMO and puppeting are not implemented or advertised. The
[protocol design](docs/protocol-design.md) explains the standards and seams for
those extensions. The [adapter contract](docs/spec/adapter.md) defines behavior;
[configuration](docs/schemas/config.schema.json) and
[native metadata](docs/schemas/native.schema.json) schemas publish the shapes.

## Develop and test

Keep the core checkout and provider checkout anywhere. From the core checkout:

```bash
uv sync --locked --extra dev
uv run --no-sync medre dev sync --sources /path/to/sources.yaml \
  --editable xmpp=/path/to/medre-adapter-xmpp xmpp
uv run --no-sync medre dev test --sources /path/to/sources.yaml \
  --editable xmpp=/path/to/medre-adapter-xmpp xmpp -- -q
```

The source catalog above adds the alias while the editable path selects the
actual checkout. Use `dev sync --sdk` to install native dependencies, then
`dev test ... xmpp -- -m xmpp_sdk -v` for pinned SDK contracts. Keep the same
catalog and editable selection on both commands; a local `.medre-dev.yaml`
profile can remember them. No temporary core branch is recorded in package
requirements, lockfiles or CI variables.

For an existing Python environment, install the chosen core checkout and this
provider using that interpreter's pip interface. The deterministic suite needs
core's dev dependencies; the SDK tier additionally needs Slixmpp 1.17.0.
CI checks released core contracts on Python 3.11–3.14 with SDK-free and native SDK
jobs. `core_ref` is a one-run qualification input, not a committed dependency.
An authenticated live-server test is separate from these offline checks.

### Opt-in native server proof

A two-account test verifies actual MUC ingress and direct egress. Use a dedicated
test server that permits temporary MUC creation. The test creates a uniquely
named room and destroys it in cleanup; it never configures an existing room.
Set `MEDRE_XMPP_TEST_CONFIG` to a private JSON file outside the checkout:

```json
{
  "bot_jid": "bot@example.org/medre",
  "bot_password": "REPLACE_IN_PRIVATE_FILE",
  "peer_jid": "peer@example.org/test",
  "peer_password": "REPLACE_IN_PRIVATE_FILE",
  "muc_domain": "conference.example.org"
}
```

Optional `host` and `port` entries override DNS discovery together. For a local
server using a dedicated test CA, `SSL_CERT_FILE` can select that trusted CA;
certificate validation and hostname checks stay enabled. Run the adapter suite
with `-m live` after installing its SDK. Credentials, private keys and runtime
state stay outside Git. The offline CI jobs do not start an authenticated server.
