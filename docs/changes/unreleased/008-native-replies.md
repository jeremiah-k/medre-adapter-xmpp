# Native XMPP reply references

- Correlate outbound room messages with trusted room-issued IDs through bounded
  own-echo waits; keep missing correlation distinct from a failed transport write.
- Release pending correlation on cancellation or disconnect and preserve direct
  message origin IDs without promoting room echoes to recipient confirmation.
