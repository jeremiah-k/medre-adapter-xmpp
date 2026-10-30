"""Lazy registration against released MEDRE contracts."""

from medre.adapter_spec import AdapterSpec, SymbolRef


def adapter_spec() -> AdapterSpec:
    return AdapterSpec(
        api_version=1,
        transport="xmpp",
        config=SymbolRef("medre.adapters.xmpp.config", "XMPPConfig"),
        runtime_config=None,
        adapter=SymbolRef("medre.adapters.xmpp.adapter", "XMPPAdapter"),
        fake_adapter=SymbolRef("medre.adapters.xmpp.fake", "FakeXMPPAdapter"),
        renderer_factory=SymbolRef("medre.adapters.xmpp.renderer", "build_renderer"),
        runtime_config_preparer=SymbolRef(
            "medre.adapters.xmpp.config", "prepare_config"
        ),
        dependency_probe=SymbolRef("medre.adapters.xmpp.compat", "HAS_SLIXMPP"),
        distribution="slixmpp",
        import_names=("slixmpp",),
        sdk_import_roots=("slixmpp", "aiodns", "pycares"),
        native_namespace_reader=SymbolRef(
            "medre.adapters.xmpp.metadata", "native_namespace"
        ),
        versioned_namespace_reader=SymbolRef(
            "medre.adapters.xmpp.metadata", "versioned"
        ),
        attribution_projector=SymbolRef(
            "medre.adapters.xmpp.metadata", "project_attribution"
        ),
        sample_config=SymbolRef("medre.adapters.xmpp.sample", "sample_config"),
        stopped_state_declaration=SymbolRef(
            "medre.adapters.xmpp.stopped_declaration", "declaration"
        ),
        support_endpoint_fields=("jid", "rooms", "peers", "host", "port"),
        support_secret_fields=("password",),
    )
