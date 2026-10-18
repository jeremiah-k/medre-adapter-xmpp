import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre_adapter_xmpp.config import XMPPConfig, bare_jid
from medre_adapter_xmpp.plugin import adapter_spec


@pytest.mark.parametrize(
    "jid",
    [
        None,
        "",
        "no-domain",
        "@example.org",
        "a@",
        "a@@example.org",
        "a@example.org/resource",
        " a@example.org",
        "a@exam ple.org",
        "a@example.org\n",
    ],
)
def test_conversations_reject_ambiguous_addresses(jid):
    with pytest.raises(ValueError):
        bare_jid(jid)


@pytest.mark.parametrize(
    "updates",
    [
        {"rooms": ["room@example.org"]},
        {"rooms": ("room@example.org", "room@example.org")},
        {"rooms": ("alice@example.org",)},
        {"peers": (1,)},
        {"port": 5222},
        {"host": "example.org"},
        {"host": "example.org", "port": True},
        {"host": "example.org", "port": 0},
        {"connect_timeout_seconds": float("nan")},
        {"reconnect_delay_seconds": 0},
        {"max_text_bytes": True},
        {"inbound_queue_size": 0},
        {"password": 42},
        {"nickname": ""},
        {"relay_prefix": "\x00"},
        {"jid": "bot@example.org/"},
    ],
)
def test_invalid_config_is_rejected(config, updates):
    with pytest.raises(ValueError):
        replace(config, **updates).validate()


def test_credentials_are_redacted_and_destinations_are_explicit(config):
    assert config.password not in repr(config)
    assert config.message_type(config.rooms[0]) == "groupchat"
    assert config.message_type(config.peers[0]) == "chat"
    with pytest.raises(ValueError, match="configured"):
        config.message_type("stranger@example.org")


def test_provider_config_loads_with_released_core(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    document = {
        "adapters": {
            "xmpp": {
                "bot": {
                    "jid": "bot@example.org/medre",
                    "rooms": ["room@example.org"],
                    "adapter_kind": "fake",
                }
            }
        },
        "routes": {},
    }
    loaded = parse_runtime_config(document, resolve())
    rtc = loaded.adapters.for_transport("xmpp")["bot"]
    assert isinstance(rtc.config, XMPPConfig)
    assert rtc.config.rooms == ("room@example.org",)
    assert rtc.adapter_kind == "fake"
    assert not (tmp_path / "state").exists()


def test_registration_config_renderer_and_fake_do_not_import_sdk():
    script = """
import importlib.abc, sys
class NoSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'slixmpp','aiodns','pycares'}:
            raise AssertionError('SDK import: ' + fullname)
sys.meta_path.insert(0, NoSDK())
from medre_adapter_xmpp.plugin import adapter_spec
s=adapter_spec()
s.config.load()
s.renderer_factory.load()
s.fake_adapter.load()
s.sample_config.load()()
print('sdk-free')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "sdk-free"


def test_native_namespace_owns_only_the_declared_version():
    spec = adapter_spec()
    data = {"xmpp": {"schema_version": 2, "nickname": "unrecognized"}}
    assert not spec.native_namespace_reader.load()(data)
    assert spec.versioned_namespace_reader.load()(data)


def test_configuration_schema_matches_dataclass_fields():
    from dataclasses import fields

    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/config.schema.json").read_text()
    )
    assert set(schema["properties"]) - {"enabled", "adapter_kind"} == {
        field.name for field in fields(XMPPConfig)
    }


def test_route_preflight_refuses_unconfigured_source_and_destination(config):
    from types import SimpleNamespace
    from medre_adapter_xmpp.config import prepare_config

    def route(source, destination):
        return SimpleNamespace(
            enabled=True,
            source=SimpleNamespace(adapter=config.adapter_id, channel=source),
            targets=[
                SimpleNamespace(
                    adapter=config.adapter_id, channel=destination, destination=None
                )
            ],
        )

    good = route(config.rooms[0], config.peers[0])
    assert (
        prepare_config(config, adapter_id=config.adapter_id, paths=None, routes=[good])
        is config
    )
    for bad in (
        route("unknown@example.org", config.peers[0]),
        route(config.rooms[0], "unknown@example.org"),
    ):
        with pytest.raises(ValueError, match="configured"):
            prepare_config(
                config, adapter_id=config.adapter_id, paths=None, routes=[bad]
            )


def test_native_schema_accepts_the_actual_codec_shape(config):
    import msgspec
    from jsonschema import Draft202012Validator
    from conftest import NOW, stanza
    from medre_adapter_xmpp.codec import XMPPCodec

    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/native.schema.json").read_text()
    )
    event = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        msgspec.to_builtins(event.metadata.native.data["xmpp"])
    )
