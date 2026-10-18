"""Lazy registration against released MEDRE contracts."""

from medre.adapter_spec import AdapterSpec, SymbolRef


def adapter_spec() -> AdapterSpec:
    return AdapterSpec(
        transport="xmpp",
        config=SymbolRef("medre_adapter_xmpp.config", "XMPPConfig"),
        runtime_config=None,
        adapter=SymbolRef("medre_adapter_xmpp.adapter", "XMPPAdapter"),
        fake_adapter=SymbolRef("medre_adapter_xmpp.fake", "FakeXMPPAdapter"),
        renderer_factory=SymbolRef("medre_adapter_xmpp.renderer", "build_renderer"),
        runtime_config_preparer=SymbolRef(
            "medre_adapter_xmpp.config", "prepare_config"
        ),
        dependency_probe=SymbolRef("medre_adapter_xmpp.compat", "HAS_SLIXMPP"),
        distribution="slixmpp",
        import_names=("slixmpp",),
        sdk_import_roots=("slixmpp", "aiodns", "pycares"),
        native_namespace_reader=SymbolRef(
            "medre_adapter_xmpp.metadata", "native_namespace"
        ),
        versioned_namespace_reader=SymbolRef(
            "medre_adapter_xmpp.metadata", "versioned"
        ),
        attribution_projector=SymbolRef(
            "medre_adapter_xmpp.metadata", "project_attribution"
        ),
        sample_config=SymbolRef("medre_adapter_xmpp.sample", "sample_config"),
        support_endpoint_fields=("jid", "rooms", "peers", "host", "port"),
        support_secret_fields=("password",),
    )
