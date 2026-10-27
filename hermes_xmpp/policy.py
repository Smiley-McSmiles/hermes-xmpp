"""Sender identity and tiering.

XMPP JIDs are compared in their bare, case-folded form (XEP-0165 style
simplification): no resource, node and domain lowercased. This is what stops
``owner@example.org/phone`` from being treated as a stranger while
``owner@example.org`` is the owner.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

OWNER = "owner"
GUEST = "guest"


def bare_jid(jid: str) -> str:
    """Return ``node@domain`` with the resource stripped and case folded."""
    text = (jid or "").strip()
    if not text:
        raise ValueError("empty JID")
    text = text.split("/", 1)[0]
    if "@" not in text:
        # A server JID (no node) is never a person; keep it distinct and folded.
        return text.lower()
    node, _, domain = text.rpartition("@")
    return f"{node.lower()}@{domain.lower()}"


def tier_for(jid: str, owners: tuple[str, ...]) -> str:
    """Classify a sender as OWNER or GUEST. Unknown senders are guests."""
    sender = bare_jid(jid)
    for owner in owners:
        try:
            if bare_jid(owner) == sender:
                return OWNER
        except ValueError:
            continue
    return GUEST


def session_key(jid: str) -> str:
    """Stable per-contact conversation key for X-Hermes-Session-Key.

    Hashed so the header never carries the contact's address, and truncated to
    keep it short; collisions across real contacts are not a practical risk.
    """
    digest = hashlib.sha256(bare_jid(jid).encode("utf-8")).hexdigest()
    return f"xmpp:{digest[:32]}"


def session_key_for(bot_jid: str, sender_jid: str) -> str:
    """Conversation/memory scope for one bot talking to one contact.

    The bot's own JID is part of the material, so two bot accounts serving the
    same operator keep separate memory scopes instead of sharing one.
    """
    material = f"{bare_jid(bot_jid)}|{bare_jid(sender_jid)}"
    return "xmpp:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# Bump when a deployment needs a fresh set of session ids: Hermes tombstones a
# deleted session id (in memory for the live gateway, and on disk as
# ``gateway.retired_session.v1.<id>``) and then refuses that id forever, so the
# only way to "reset" a conversation server-side is a new derived id.
SESSION_ID_VERSION = "3"


def session_id_for(bot_jid: str, sender_jid: str) -> str:
    """The one Hermes session this contact uses with this bot.

    Derived from the *bare* JID, so every device of a person lands in the same
    transcript and context: one conversation per user, not one per resource.
    Deriving it (instead of remembering a returned id) also means a bridge
    restart, a lost state file or a pruned session can never fork the user into
    a second conversation.
    """
    material = f"{bare_jid(bot_jid)}|{bare_jid(sender_jid)}|v{SESSION_ID_VERSION}"
    return "xmpp-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def is_ignored(jid: str, ignore_jids: tuple[str, ...]) -> bool:
    """True when this sender is on the do-not-answer list (other bridge bots)."""
    sender = bare_jid(jid)
    for entry in ignore_jids:
        try:
            if bare_jid(entry) == sender:
                return True
        except ValueError:
            continue
    return False


@dataclass
class RateLimiter:
    """Per-sender token bucket: ``limit`` messages per rolling minute."""

    limit: int = 8
    window_s: float = 60.0

    def __post_init__(self) -> None:
        self._sent: dict[str, list[float]] = {}

    def allow(self, jid: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        key = bare_jid(jid)
        stamps = [t for t in self._sent.get(key, []) if now - t < self.window_s]
        if len(stamps) >= self.limit:
            self._sent[key] = stamps
            return False
        stamps.append(now)
        self._sent[key] = stamps
        return True
