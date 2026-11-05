"""SDK-free native form metadata remains provider-owned and deterministic."""

import stat

import pytest

from medre.adapters.xmpp.plugin import adapter_spec


def test_native_setup_fields_are_sdk_free_and_named_for_the_config_model():
    spec = adapter_spec()
    fields = spec.native_setup_fields
    names = {field.name for field in fields}
    config_model = spec.config.load()
    from dataclasses import fields as model_fields

    assert names <= {field.name for field in model_fields(config_model)}
    assert len(names) == len(fields)
    assert {"jid", "password", "nickname", "host", "port"} == names
    jid = next(field for field in fields if field.name == "jid")
    assert jid.required
    assert not jid.choices
    assert not jid.numeric
    for field in fields:
        assert not field.active_when


def test_password_uses_hidden_optional_input():
    password = next(
        field
        for field in adapter_spec().native_setup_fields
        if field.name == "password"
    )
    assert password.secret and not password.required


def test_native_secret_matches_provider_redaction_metadata():
    spec = adapter_spec()
    secret_names = {field.name for field in spec.native_setup_fields if field.secret}
    assert secret_names == set(spec.support_secret_fields)


def test_connection_overrides_stay_optional_with_dns_srv_default():
    fields = {field.name: field for field in adapter_spec().native_setup_fields}
    assert not fields["host"].required and not fields["host"].numeric
    assert not fields["port"].required and fields["port"].numeric
    assert not fields["nickname"].required and not fields["nickname"].secret


def _run_native_form(monkeypatch, tmp_path, answers, secret=""):
    import io
    from argparse import Namespace

    from medre.cli import native_commands
    from medre.config._yaml import parse_yaml_config

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output = tmp_path / "connection.yaml"
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", Terminal())
    replies = iter(answers)
    monkeypatch.setattr(native_commands, "_ask", lambda *args: next(replies))
    monkeypatch.setattr(native_commands.getpass, "getpass", lambda *args: secret)
    monkeypatch.setattr("medre.cli.bridge_commands._confirm_save", lambda *args: True)
    native_commands._config_native(
        Namespace(transport="xmpp", adapter_id="chat", config=None, output=str(output))
    )
    assert not (tmp_path / "state").exists()
    return output, parse_yaml_config(output.read_text())["adapters"]["xmpp"]["chat"]


def test_native_form_validates_a_complete_xmpp_connection(monkeypatch, tmp_path):
    output, values = _run_native_form(
        monkeypatch,
        tmp_path,
        ["bridge@example.org/medre", "medre", "xmpp.example.org", "5222"],
        secret="test-only-password",
    )
    assert values["adapter_kind"] == "real"
    assert values["jid"] == "bridge@example.org/medre"
    assert values["password"] == "test-only-password"
    assert values["nickname"] == "medre"
    assert values["host"] == "xmpp.example.org"
    assert values["port"] == 5222
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_native_form_keeps_an_unset_password_out_of_the_file(monkeypatch, tmp_path):
    output, values = _run_native_form(
        monkeypatch,
        tmp_path,
        ["bridge@example.org/medre", "", "", ""],
    )
    assert values["jid"] == "bridge@example.org/medre"
    assert "password" not in values
    assert "password" not in output.read_text()


@pytest.mark.parametrize(
    "answers",
    [
        # Host override without its port pair.
        ["bridge@example.org/medre", "", "xmpp.example.org", ""],
        # A mixed-case bare JID never matches a prepared server identity.
        ["Room@Conference.example.org", "", "", ""],
    ],
)
def test_native_form_rejects_invalid_connection_values(monkeypatch, tmp_path, answers):
    with pytest.raises(SystemExit) as excinfo:
        _run_native_form(monkeypatch, tmp_path, answers)
    assert excinfo.value.code == 2
    assert not (tmp_path / "connection.yaml").exists()
