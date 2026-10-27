import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from hermes_xmpp import bot as botmod

MUC = "http://jabber.org/protocol/muc#user"


def _muc_invite(room="room@conference.example.org", inviter="owner@example.org/phone"):
    return ET.fromstring(
        f'<message from="{room}" type="normal" xmlns="jabber:client">'
        f'<x xmlns="{MUC}"><invite from="{inviter}"><reason>come in</reason></invite></x>'
        f"</message>")


def _direct_invite(room="room@conference.example.org", inviter="owner@example.org/phone"):
    return ET.fromstring(
        f'<message from="{inviter}" type="normal" xmlns="jabber:client">'
        f'<x xmlns="jabber:iq:conference" jid="{room}" password=""/></message>')


def _bridge(*, owners=("owner@example.org",), accept=True, join_error=None):
    joined, notified = [], []

    class FakeMuc:
        async def join_muc_wait(self, room, nick, **kwargs):
            if join_error:
                raise RuntimeError(join_error)
            joined.append((str(room), nick, kwargs))

    async def fake_notify(to, text):
        notified.append((to, text))

    async def notify_owners(text):
        for owner in owners:
            await fake_notify(owner, text)

    fake_self = SimpleNamespace(
        bot_bare="bot@example.org",
        cfg=SimpleNamespace(
            rooms=SimpleNamespace(nick="bot", accept_invites=accept),
            policy=SimpleNamespace(owners=owners),
        ),
        plugin={"xep_0045": FakeMuc()},
        _notify=fake_notify,
        _notify_owners=notify_owners,
    )
    return fake_self, joined, notified


# -- parsing -----------------------------------------------------------------

def test_mediated_muc_invite_is_parsed():
    assert botmod.room_invite(_muc_invite()) == (
        "room@conference.example.org", "owner@example.org")


def test_direct_xep0249_invite_is_parsed():
    assert botmod.room_invite(_direct_invite()) == (
        "room@conference.example.org", "owner@example.org")


def test_ordinary_message_is_not_an_invite():
    xml = ET.fromstring('<message from="a@b" type="chat" xmlns="jabber:client">'
                        "<body>hello</body></message>")
    assert botmod.room_invite(xml) is None


# -- addressing --------------------------------------------------------------

def test_room_addressing_accepts_nick_localpart_and_hermes():
    fake_self, _joined, _notified = _bridge()
    fake_self.cfg.account = SimpleNamespace(jid="bot@example.org")
    fake_self.cfg.rooms.respond = "mention"
    for body in ("bot: are you there", "@bot ping", "bot, status?", "hey bot",
                 "hey @bot can you see this", "@bot, you there?", "**bot** hello",
                 "so @bot what do you think"):
        assert botmod.Bridge._room_addressed(fake_self, body)[0] is True
    # the mention is stripped, the rest of the sentence is kept
    assert botmod.Bridge._room_addressed(fake_self, "hey @bot can you see this") == (
        True, "hey can you see this")
    assert botmod.Bridge._room_addressed(fake_self, "hermes: hi")[0] is True
    assert botmod.Bridge._room_addressed(fake_self, "just chatting")[0] is False


# -- behaviour ---------------------------------------------------------------

def test_owner_invite_joins_the_room_and_confirms_to_the_inviter():
    fake_self, joined, notified = _bridge()
    room, inviter = botmod.room_invite(_muc_invite())
    asyncio.run(botmod.Bridge._handle_room_invite(fake_self, room, inviter))
    assert joined == [("room@conference.example.org", "bot",
                       {"maxstanzas": 0, "timeout": 15})]
    assert notified[0][0] == "owner@example.org"
    assert "Joined room@conference.example.org as bot" in notified[0][1]


def test_stranger_invite_is_reported_not_joined():
    fake_self, joined, notified = _bridge()
    room, inviter = botmod.room_invite(_muc_invite(inviter="stranger@example.org/x"))
    asyncio.run(botmod.Bridge._handle_room_invite(fake_self, room, inviter))
    assert joined == []
    assert "Not an owner" in notified[0][1]
    assert notified[0][0] == "owner@example.org"


def test_invites_can_be_switched_off():
    fake_self, joined, notified = _bridge(accept=False)
    room, inviter = botmod.room_invite(_muc_invite())
    asyncio.run(botmod.Bridge._handle_room_invite(fake_self, room, inviter))
    assert joined == []
    assert "Room invites are off" in notified[0][1]


def test_failed_join_is_reported_to_the_inviter():
    fake_self, joined, notified = _bridge(join_error="nickname in use")
    room, inviter = botmod.room_invite(_muc_invite())
    asyncio.run(botmod.Bridge._handle_room_invite(fake_self, room, inviter))
    assert joined == []
    assert any("could not join" in text for _to, text in notified)
