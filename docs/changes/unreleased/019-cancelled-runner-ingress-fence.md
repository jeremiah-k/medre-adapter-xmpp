# Fence native ingress after unexpected runner cancellation

An unexpectedly cancelled XMPP runner leaves an abandoned session with failed
health and retained ownership. Native callbacks cannot queue fresh ingress until
an explicit stop retires the session; shutdown-owned cancellation retains its
existing cleanup behavior.
