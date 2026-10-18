# Authentication, MUC identity and reply references

- Only exhausted SASL authentication mechanisms constitute a terminal
  authentication failure; fallback mechanism attempts remain possible.
- Keep unexpected room occupant identity as a permanent configuration error,
  rather than converting it to a reconnectable send error.
- Prefer the room-issued XEP-0359 stanza ID in groupchat native references,
  retaining sender-provided message IDs separately in the native metadata.
  This lets incoming XEP-0461 room replies address the correct native ID
  namespace. Without a room-issued ID, no groupchat reply native reference is
  registered. Unknown or conflicting room authorities are not promoted.
- Update the strict native metadata schema and offline regression coverage.
