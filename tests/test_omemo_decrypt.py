"""The decrypt helpers return text, not a stanza (a Message has no .strip())."""

import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from hermes_xmpp import omemo


class _Stanza:
    def __init__(self, inner: str = ""):
        self.xml = ET.fromstring("<message xmlns='jabber:client' type='chat'>%s</message>" % inner)

    def __getitem__(self, key):
        return {"from": "owner@example.org/phone", "body": ""}[key]


class _Plugin:
    def __init__(self, body="hello there"):
        self._body = body

    async def decrypt_message(self, stanza):
        return {"body": self._body}, SimpleNamespace(device_id=7)


def _xmpp(plugin):
    return SimpleNamespace(plugin={"xep_0384": plugin},
                           boundjid=SimpleNamespace(bare="bot@example.org"))


def test_legacy_decrypt_returns_the_body_text():
    body, device = asyncio.run(omemo.decrypt_one_to_one(_xmpp(_Plugin()), _Stanza()))
    assert isinstance(body, str)
    assert body == "hello there"
    assert device.device_id == 7


def test_a_message_with_both_versions_uses_the_legacy_envelope():
    sces = ("<encrypted xmlns='urn:xmpp:omemo:2'/>"
            "<encrypted xmlns='eu.siacs.conversations.axolotl'/>")
    body, _device = asyncio.run(
        omemo.decrypt_one_to_one(_xmpp(_Plugin("from the legacy envelope")), _Stanza(sces)))
    assert body == "from the legacy envelope"
