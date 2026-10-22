# XEP-0393 message styling

- Map bounded XMPP blocks and inline spans to canonical semantic presentation,
  preserving literal directives and honoring the unstyled control.
- Render semantic inline styles only when parsing the complete wire text
  reproduces the intended spans; preserve plain text for unsupported styles,
  delimiter collisions, prefix conflicts, and formatting budget overhead.
- Advertise styling support and serialize an explicit unstyled control when
  plain content must remain literal. Preserve reply and attribution boundaries.
