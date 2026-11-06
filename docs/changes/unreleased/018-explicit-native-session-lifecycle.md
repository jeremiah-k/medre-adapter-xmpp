# Make the XMPP native session lifecycle explicit and cancellation-safe

Replace implicit accepting/task combinations with a checked session phase.
Bound stop waiting even when SDK coroutines ignore cancellation; retain the
unretired task as owned work and refuse reuse until cleanup completes. Prevent
retired connection callbacks from restoring healthy state or starting another
reconnect. Native recipient evidence and retry authority are unchanged.

Shutdown uses one deadline for cleanup-lock acquisition and owned work; invalid
budgets leave health observation unchanged. Failed or interrupted native cleanup
retains the connection and fences reconnect/reuse until a cleanup retry succeeds.
Startup cancellation propagates even when cleanup cannot retire native work.
Stopped ingress discards queued stanzas and unstarted typing batch entries.
