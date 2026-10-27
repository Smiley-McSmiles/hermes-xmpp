"""OMEMO wiring: one plugin, both protocol versions.

slixmpp-omemo's XEP-0384 plugin speaks legacy OMEMO 0.3
(eu.siacs.conversations.axolotl) and OMEMO 2 / 0.8 (urn:xmpp:omemo:2)
transparently, picking the right version per recipient device. It is abstract:
we supply persistent storage, and a trust policy.

Trust policy here is blind trust before verification (BTBV) with automatic
acceptance, which matches "auto-add anyone who asks": new devices are usable
immediately, and the bridge logs when that happens so the operator can audit.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import FrozenSet, Optional
from xml.etree import ElementTree as ET

from omemo.storage import Storage
from omemo.types import DeviceInformation
from slixmpp_omemo import TrustLevel, XEP_0384
from slixmpp_omemo.base_session_manager import BaseSessionManager

from . import sce
from .storage import JSONFileStorage

log = logging.getLogger(__name__)

CLIENT_NS = "jabber:client"


def _twomemo():
    """The OMEMO 2 backend (its import compiles three XML schemas; keep it lazy)."""
    import twomemo
    import twomemo.etree  # noqa: F401  (registers the parser/serializer)
    return twomemo


async def encrypt_group(xmpp, room: str, text: str, recipients: set[str], own_bare: str):
    """A groupchat stanza whose body is encrypted with OMEMO 2 for every member.

    slixmpp-omemo's ``encrypt_message`` only ever produces legacy OMEMO (the SCE half
    is unimplemented), so the envelope is built here and handed to python-omemo's
    session manager directly. Recipients are the bare JIDs of the room's members;
    the sender is not one of them (reflections carry no key for us).
    """
    from slixmpp import JID

    twomemo = _twomemo()
    plugin = xmpp.plugin["xep_0384"]
    targets = {str(JID(r).bare) for r in recipients if r}
    targets.discard(own_bare)
    if not targets:
        raise ValueError("the room has no other members with a known address")
    await plugin.refresh_device_lists({JID(j) for j in targets})
    body = ET.Element("{%s}body" % CLIENT_NS)
    body.text = text
    envelope = sce.build([body], to_jid=room, from_jid=own_bare)
    manager = await plugin.get_session_manager()
    try:
        messages, errors = await manager.encrypt(
            frozenset(targets),
            {twomemo.twomemo.NAMESPACE: envelope},
            backend_priority_order=[twomemo.twomemo.NAMESPACE],
            identifier=room,
        )
    except Exception as exc:  # noqa: BLE001 - a member with only legacy device data
        log.info("OMEMO 2 is not usable in %s (%s); trying legacy OMEMO", room, exc)
        return await _legacy_group(xmpp, room, text, targets)
    for error in errors:
        log.debug("group OMEMO error for %s: %s", room, error)
    stanza = xmpp.make_message(mto=room, mtype="groupchat")
    stanza["body"] = "This message is encrypted."
    stanza.enable("store")
    sent = 0
    for message in messages:
        if message.namespace != twomemo.twomemo.NAMESPACE:
            continue
        stanza.append(twomemo.etree.serialize_message(message))
        sent += 1
    if not sent:
        log.info("no OMEMO 2 device in %s; trying legacy OMEMO", room)
        return await _legacy_group(xmpp, room, text, targets)
    ET.SubElement(stanza.xml, "{urn:xmpp:eme:0}eme",
                  {"namespace": twomemo.twomemo.NAMESPACE})
    return stanza


async def trust_room_devices(xmpp, targets: set[str]) -> int:
    """Mark every known device of each member as trusted, for all backends.

    Group encryption refuses to run unless each member has at least one *trusted*
    device. Automatic trust only covers the devices discovered while a backend
    handled a message, so a member who speaks the other OMEMO version looks like
    they have no device at all and the whole room reply is refused.
    """
    from slixmpp import JID

    plugin = xmpp.plugin["xep_0384"]
    try:
        await plugin.refresh_device_lists({JID(j) for j in targets})
    except Exception as exc:  # noqa: BLE001 - stale lists are better than none
        log.debug("could not refresh device lists for %s: %s", targets, exc)
    manager = await plugin.get_session_manager()
    trusted = 0
    for jid in targets:
        try:
            devices = await manager.get_device_information(JID(jid))
        except Exception as exc:  # noqa: BLE001 - one bad list must not stop the rest
            log.debug("no device list for %s: %s", jid, exc)
            continue
        for device in devices or ():
            try:
                await manager.set_trust(JID(jid), device.identity_key,
                                        TrustLevel.TRUSTED.value)
                trusted += 1
            except Exception:  # noqa: BLE001 - an already-trusted device is fine
                continue
    if trusted:
        log.info("trusted %d device(s) for room encryption: %s", trusted, sorted(targets))
    return trusted


async def _legacy_group(xmpp, room: str, text: str, targets: set[str]):
    """Fallback for a room whose members only have legacy OMEMO device data.

    Legacy OMEMO carries just the body, but it is still encrypted for every member
    and Ward decrypts it; refusing outright left the room unable to answer at all.
    """
    from slixmpp import JID

    plugin = xmpp.plugin["xep_0384"]
    stanza = xmpp.make_message(mto=room, mtype="groupchat", mbody=text)
    encrypted, errors = await plugin.encrypt_message(
        stanza, {JID(j) for j in targets}, identifier=room)
    if encrypted is None:
        raise ValueError("no member device could receive an encrypted message")
    log.warning("room %s is only legacy-OMEMO capable (%d device errors)", room, len(errors))
    return encrypted


def _body_text(value) -> str:
    """Plain text of a decrypted body, whichever shape the plugin returned.

    ``decrypt_message`` hands back a stanza rather than a string for some
    versions, and a stanza's ``body`` can itself be a ``xml:lang`` mapping.
    Anything that is not text has to be unwrapped here, or the caller ends up
    calling ``.strip()`` on a Message and the whole turn dies.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        inner = value["body"]
    except Exception:  # noqa: BLE001 - not a stanza at all
        return ""
    if inner is value or inner is None:
        return ""
    if isinstance(inner, str):
        return inner
    if isinstance(inner, dict):
        for text in inner.values():
            if isinstance(text, str) and text:
                return text
        return ""
    return str(inner)


async def decrypt_group(xmpp, stanza) -> str:
    """Plaintext body of a room message, from an OMEMO 2 (SCE) or legacy envelope."""
    from slixmpp import JID

    plugin = xmpp.plugin["xep_0384"]
    room = str(stanza["from"].bare)
    nick = str(stanza["from"].resource)
    real_jid = xmpp.plugin["xep_0045"].get_jid_property(JID(room), nick, "jid")
    if not real_jid:
        raise ValueError("the room is anonymous, so the sender cannot be verified")
    sender = str(JID(real_jid).bare)
    twomemo = _twomemo()
    encrypted = stanza.xml.find("{%s}encrypted" % twomemo.twomemo.NAMESPACE)
    if encrypted is None:
        plain, _device = await plugin.decrypt_message(stanza)
        return _body_text(plain)
    message = twomemo.etree.parse_message(encrypted, sender)
    manager = await plugin.get_session_manager()
    plaintext, _device, _extra = await manager.decrypt(message)
    if plaintext is None:
        return ""
    return sce.body_of(sce.parse(plaintext, expect_from=sender, expect_to=room))


async def decrypt_one_to_one(xmpp, stanza):
    """``(body, device)`` for a direct message of either OMEMO version.

    A peer that speaks both sends one envelope per version; the plugin refuses the
    OMEMO 2 one outright, so the legacy envelope is decrypted instead of failing the
    whole message. A peer that sends only OMEMO 2 is unwrapped here.
    """
    from slixmpp import JID

    plugin = xmpp.plugin["xep_0384"]
    twomemo = _twomemo()
    sces = stanza.xml.findall("{%s}encrypted" % twomemo.twomemo.NAMESPACE)
    if not sces:
        plain, device = await plugin.decrypt_message(stanza)
        return _body_text(plain), device
    legacy = stanza.xml.findall("{eu.siacs.conversations.axolotl}encrypted")
    if legacy:
        stripped = copy.copy(stanza)
        for element in stripped.xml.findall("{%s}encrypted" % twomemo.twomemo.NAMESPACE):
            stripped.xml.remove(element)
        plain, device = await plugin.decrypt_message(stripped)
        return _body_text(plain), device
    sender = str(stanza["from"].bare)
    manager = await plugin.get_session_manager()
    message = twomemo.etree.parse_message(sces[0], sender)
    plaintext, device, _extra = await manager.decrypt(message)
    if plaintext is None:
        raise ValueError("empty OMEMO 2 message")
    body = sce.body_of(sce.parse(plaintext, expect_from=sender,
                                 expect_to=str(xmpp.boundjid.bare)))
    return _body_text(body), device


class FileBackedOmemo(XEP_0384):
    """XEP-0384 plugin with a JSON-file store and automatic trust."""

    _storage_path: Path = Path("~/.hermes-xmpp/omemo.json")
    _storage_instance: Storage | None = None

    @property
    def storage(self) -> Storage:
        if type(self)._storage_instance is None:
            type(self)._storage_instance = JSONFileStorage(type(self)._storage_path)
        return type(self)._storage_instance

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        for device in blindly_trusted:
            log.warning(
                "auto-trusted new OMEMO device %s of %s (fingerprint %s)",
                device.device_id,
                device.bare_jid,
                device.identity_key.hex()[:16],
            )

    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        # Reached only for identities that were explicitly distrusted before;
        # the operator asked for automatic acceptance, so trust and move on.
        session_manager = await self.get_session_manager()
        for device in manually_trusted:
            await session_manager.set_trust(
                device.bare_jid, device.identity_key, TrustLevel.TRUSTED.value
            )
            log.warning("manually trusted OMEMO device %s of %s", device.device_id, device.bare_jid)


def install(xmpp, storage_path: str) -> None:
    """Register the plugin class globally, then enable it on this client."""
    from slixmpp.plugins.base import register_plugin as register_plugin_class

    FileBackedOmemo._storage_path = Path(storage_path).expanduser()
    register_plugin_class(FileBackedOmemo)

    for dependency in ("xep_0004", "xep_0030", "xep_0060", "xep_0163", "xep_0280", "xep_0334"):
        try:
            xmpp.register_plugin(dependency)
        except Exception:  # noqa: BLE001 - already loaded is fine
            log.debug("plugin %s already registered", dependency)
    xmpp.register_plugin("xep_0384")


__all__ = ["FileBackedOmemo", "install", "BaseSessionManager"]
