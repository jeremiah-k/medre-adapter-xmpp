# SDK-free context sizing

Keep offline rendering and preview free of Slixmpp imports. Context sizing uses
the native serializer after startup has loaded it, and otherwise uses the
standard-library serializer without starting or loading the transport SDK.
