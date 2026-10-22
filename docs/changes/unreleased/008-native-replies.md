# Native XMPP reply references

- Correlate outbound room messages with trusted room-issued IDs through bounded
  own-echo waits; keep missing correlation distinct from a failed transport write.
- Release pending correlation on cancellation or disconnect and preserve direct
  message origin IDs without promoting room echoes to recipient confirmation.
- Bridge native replies with scoped target IDs and Unicode-safe compatibility
  quotations; retain readable fallback when a native target cannot be bound.
- Preserve direct origin IDs independently of rewritten stanza IDs, validate
  rendered reply fields, and ignore fallback-only bodies as message content.
