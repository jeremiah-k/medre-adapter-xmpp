"""Offline sample values; no account is contacted or provisioned."""

from medre.adapter_spec import AdapterSample


def sample_config() -> AdapterSample:
    return AdapterSample(
        {
            "jid": "medre@example.org/bridge",
            "rooms": ["bridge@conference.example.org"],
            "origin_label": "XMPP",
        },
        channel="bridge@conference.example.org",
    )
