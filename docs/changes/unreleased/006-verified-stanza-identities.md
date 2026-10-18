# Verified XMPP stanza identity scopes

- Verify room stanza-ID support through XEP-0030 before using room IDs or
  reply references, and retain that connection evidence with queued stanzas.
- Keep ordinary text available when discovery is unsupported or times out.
- Scope client-assigned IDs to full sending JIDs while retaining bare direct
  conversation identity; scope verified room IDs to the assigning room.
