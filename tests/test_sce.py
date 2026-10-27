"""The SCE (XEP-0420) envelope the bridge uses for OMEMO 2 in rooms."""

import time
from xml.etree import ElementTree as ET

import pytest

from hermes_xmpp import sce


def _body(text: str = "hello room") -> ET.Element:
    el = ET.Element("{jabber:client}body")
    el.text = text
    return el


def test_build_and_parse_roundtrip():
    data = sce.build([_body()], to_jid="room@conference.example.org", from_jid="bot@example.org")
    content = sce.parse(data, expect_from="bot@example.org",
                        expect_to="room@conference.example.org")
    assert sce.body_of(content) == "hello room"


def test_parse_ignores_resources_and_case():
    data = sce.build([_body()], to_jid="room@conference.example.org", from_jid="bot@example.org")
    content = sce.parse(data, expect_from="Bot@Example.org/phone",
                        expect_to={"other@x", "Room@Conference.Example.org/laptop"})
    assert sce.body_of(content) == "hello room"


def test_parse_rejects_a_foreign_sender_or_target():
    data = sce.build([_body()], to_jid="room@conference.example.org", from_jid="bot@example.org")
    with pytest.raises(sce.EnvelopeError, match="someone else"):
        sce.parse(data, expect_from="eve@example.org", expect_to="room@conference.example.org")
    with pytest.raises(sce.EnvelopeError, match="another conversation"):
        sce.parse(data, expect_from="bot@example.org", expect_to="elsewhere@example.org")


def test_parse_rejects_a_future_envelope():
    data = sce.build([_body()], to_jid="room@x", from_jid="bot@x",
                     now=time.time() + sce.MAX_SKEW + 60)
    with pytest.raises(sce.EnvelopeError, match="future"):
        sce.parse(data, expect_from="bot@x", expect_to="room@x")


def test_parse_refuses_xml_with_a_doctype():
    evil = (b'<!DOCTYPE x [<!ENTITY a "b">]><envelope xmlns="urn:xmpp:sce:1">'
            b"<content xmlns=\"urn:xmpp:sce:1\"><body xmlns=\"jabber:client\">x</body>"
            b"</content></envelope>")
    with pytest.raises(sce.EnvelopeError, match="document type"):
        sce.parse(evil, expect_from="bot@x", expect_to="room@x")


def test_body_of_is_empty_without_a_body():
    assert sce.body_of([]) == ""
    assert sce.body_of([ET.fromstring("<content xmlns='urn:xmpp:sce:1'/>")]) == ""


def test_config_parses_rooms_encrypt(tmp_path):
    from hermes_xmpp.config import load

    (tmp_path / "secrets").mkdir()
    for name in ("account.secret", "owner.key", "guest.key"):
        p = tmp_path / "secrets" / name
        p.write_text("value")
        p.chmod(0o600)
    toml = """
[account]
jid = "bot@example.org"
password_file = "secrets/account.secret"

[owner]
url = "http://127.0.0.1:8642/v1/chat/completions"
key_file = "secrets/owner.key"

[guest]
url = "http://127.0.0.1:8642/p/xmpp-guest/v1/chat/completions"
key_file = "secrets/guest.key"

[rooms]
join = ["room@conference.example.org"]
encrypt = true
"""
    path = tmp_path / "hermes.toml"
    path.write_text(toml)
    assert load(path).rooms.encrypt is True
