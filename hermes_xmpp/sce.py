"""Stanza Content Encryption (XEP-0420): the envelope OMEMO 2 encrypts.

slixmpp-omemo implements neither side of the OMEMO 2 envelope ("SCE not supported
yet"), so the bridge builds and parses the small subset it needs: a ``<content>``
holding the original body, with the ``to``/``from`` affixes a receiver checks so a
message cannot be replayed into another conversation. Ward builds the same shape
(``ward/core/omemo/sce.py``), and that is what makes a group chat interoperable.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import time
from xml.etree import ElementTree as ET

NS = "urn:xmpp:sce:1"
CLIENT_NS = "jabber:client"
MAX_SKEW = 7 * 24 * 3600


class EnvelopeError(Exception):
    """The envelope is not ours: malformed, replayed, or addressed elsewhere."""


def build(content: list[ET.Element], to_jid: str, from_jid: str, now: float | None = None) -> bytes:
    env = ET.Element("{%s}envelope" % NS)
    box = ET.SubElement(env, "{%s}content" % NS)
    for el in content:
        box.append(el)
    # 0..199 random characters, as the spec recommends to hide the real length
    ET.SubElement(env, "{%s}rpad" % NS).text = secrets.token_urlsafe(150)[:secrets.randbelow(200)]
    stamp = _dt.datetime.fromtimestamp(now if now is not None else time.time(), _dt.UTC)
    ET.SubElement(env, "{%s}time" % NS, {"stamp": stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    ET.SubElement(env, "{%s}to" % NS, {"jid": to_jid})
    ET.SubElement(env, "{%s}from" % NS, {"jid": from_jid})
    return ET.tostring(env, encoding="utf-8")


def parse(data: bytes, *, expect_from: str, expect_to: str | set[str]) -> list[ET.Element]:
    """Content elements of an envelope addressed from/to the given bare JIDs."""
    if not data or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise EnvelopeError("refusing an empty envelope or one with a document type")
    try:
        env = ET.fromstring(data)
    except ET.ParseError as exc:
        raise EnvelopeError("not XML: %s" % exc) from None
    if env.tag != "{%s}envelope" % NS:
        raise EnvelopeError("not an SCE envelope")
    sender = env.find("{%s}from" % NS)
    target = env.find("{%s}to" % NS)
    if sender is None or _bare(sender.get("jid")) != _bare(expect_from):
        raise EnvelopeError("the envelope was sent by someone else")
    allowed = {_bare(expect_to)} if isinstance(expect_to, str) else {_bare(t) for t in expect_to}
    if target is None or _bare(target.get("jid")) not in allowed:
        raise EnvelopeError("the envelope was meant for another conversation")
    stamp = env.find("{%s}time" % NS)
    if stamp is not None:
        when = _stamp(stamp.get("stamp"))
        if when is not None and when - time.time() > MAX_SKEW:
            raise EnvelopeError("the envelope is dated in the future")
    content = env.find("{%s}content" % NS)
    return list(content) if content is not None else []


def body_of(elements: list[ET.Element]) -> str:
    """The message body inside an envelope's content, '' when it has none."""
    for el in elements:
        if el.tag == "{%s}body" % CLIENT_NS and el.text:
            return el.text
    return ""


def _bare(jid: str | None) -> str:
    return (jid or "").split("/", 1)[0].strip().lower()


def _stamp(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
