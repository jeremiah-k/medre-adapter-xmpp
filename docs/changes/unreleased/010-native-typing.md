# Native typing notifications

Forward opt-in transient composing activity through XEP-0085 for configured
rooms and negotiated direct contacts. Coalesce activity independently from
ordinary message ingress, suppress repeated standalone composing states, and
keep content-driven active transitions consistent with subsequent activity.

Keep native SDK ownership in the connection, bound actor and conversation
state, and leave canonical history and delivery receipts unchanged. XMPP has
no server-managed typing timeout; clear delivery remains best effort.
