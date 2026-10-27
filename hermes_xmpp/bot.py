"""The XMPP side: 1:1 chats, MUC rooms, OMEMO, HTTP uploads."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import slixmpp
from slixmpp import JID

from . import approvals, hermes_api, omemo, reactions, uploads
from .chunk import split_message
from .config import Config, Endpoint, read_secret
from .policy import (GUEST, OWNER, RateLimiter, bare_jid, is_ignored,
                     session_id_for, session_key_for, tier_for)

log = logging.getLogger("hermes_xmpp.bot")

OMEMO2_NAMESPACE = "urn:xmpp:omemo:2"
LEGACY_NAMESPACE = "eu.siacs.conversations.axolotl"
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGES_PER_TURN = 4


@dataclass
class _Chat:
    """Everything in flight for one conversation (a contact, or a room member).

    At most one model run exists per conversation at a time. A message that
    arrives while the model is still working interrupts that run and is folded
    into the next one, so a person never gets two parallel answers and the
    Hermes session never forks.
    """

    pending: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    run_id: str | None = None
    endpoint: Endpoint | None = None
    api_key: str = ""
    interrupt_requested: bool = False
    suppress: bool = False   # drop the reply of the run we just interrupted
    media_turn: bool = False  # this turn carries an image: do not interrupt it


MUC_USER_NS = "http://jabber.org/protocol/muc#user"
DIRECT_INVITE_NS = "jabber:iq:conference"
MENTION_STRIP = ".,:;!?()[]<>\"'@*_~"


def _mention_word(word: str) -> str:
    """A word with mention punctuation and decoration removed, lowercased."""
    return word.strip(MENTION_STRIP).lower()


def room_invite(xml) -> tuple[str, str] | None:
    """(room, inviter) from a mediated MUC invite or a direct XEP-0249 one.

    A MUC invitation is a message with no body, so the ordinary message path
    used to drop it. Both spellings are recognised: Prosody and most rooms use
    ``<x xmlns="...muc#user"><invite from="..."/></x>``, while some clients send
    a lighter ``<x xmlns="jabber:iq:conference" jid="room@conference.host"/>``.
    """
    x = xml.find(f"{{{MUC_USER_NS}}}x")
    if x is not None:
        invite = x.find(f"{{{MUC_USER_NS}}}invite")
        if invite is not None:
            room = bare_jid(str(xml.get("from") or ""))
            inviter = bare_jid(str(invite.get("from") or xml.get("from") or ""))
            if room:
                return room, inviter
    direct = xml.find(f"{{{DIRECT_INVITE_NS}}}x")
    if direct is not None:
        room = bare_jid(str(direct.get("jid") or ""))
        inviter = bare_jid(str(xml.get("from") or ""))
        if room:
            return room, inviter
    return None


def _is_stop_command(text: str) -> bool:
    """A bare ``stop`` never reaches the model; anything else is conversation."""
    return (text or "").strip().lower() == "stop"


def inline_image(path: Path, max_mb: int) -> str | None:
    """``data:`` URL for a small image on disk; None when it is not one or too large."""
    mime = IMAGE_MIME.get(path.suffix.lower())
    if mime is None:
        return None
    try:
        if path.stat().st_size > max_mb * 1024 * 1024:
            return None
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    except OSError:
        return None

DEFAULT_OWNER_PROMPT = (
    "You are Hermes, answering over XMPP. The sender is an authorized operator on "
    "this machine, so your full toolset is available. Your reply is delivered "
    "verbatim as one or more XMPP chat messages: use plain text, keep code in "
    "fenced blocks, and avoid tables or HTML. Answer directly, without preamble. "
    "To send a local file, put [[attach: /absolute/path]] in the reply. To react "
    "to the message you are answering, put [[react: 👍]] on its own line: the "
    "bridge sends it as a reaction and removes the marker."
)

DEFAULT_GUEST_PROMPT = (
    "You are Hermes, answering over XMPP to a contact who is not authorized for "
    "this machine. You have no tools in this conversation. Never claim to run "
    "commands, read files, browse, or change anything on the host; if a request "
    "needs the operator's machine, say briefly that it is not available to this "
    "contact and offer a general answer instead. Your reply is delivered verbatim "
    "as one or more XMPP chat messages: plain text only, no tables or HTML."
)


class Bridge(slixmpp.ClientXMPP):
    def __init__(self, cfg: Config, owner_key: str, guest_key: str) -> None:
        super().__init__(cfg.account.jid, read_secret(cfg.account.password_file))
        self.cfg = cfg
        self.owner_key = owner_key
        self.guest_key = guest_key
        self.bot_bare = bare_jid(cfg.account.jid)
        self.limiter = RateLimiter(cfg.policy.rate_limit_per_minute)
        self._chats: dict[str, _Chat] = {}
        # Per-conversation override for a session id the server has retired
        # (deleted/pruned sessions are tombstoned and refused forever).
        self._session_ids: dict[str, str] = {}
        self._stopped = asyncio.Event()
        self._upload_host: str = ""
        _instance = bare_jid(cfg.account.jid).split('@', 1)[0]
        self._pending = approvals.PendingStore(
            Path(cfg.features.omemo_storage).expanduser().with_name(f"pending-{_instance}.json"))
        self._spool = Path(cfg.features.spool_dir).expanduser()
        # Last time we told the owners a room's OMEMO is unreadable (room -> epoch).
        self._room_warned: dict[str, float] = {}

        self.register_plugin("xep_0045")           # multi-user chat
        self.register_plugin("xep_0066")           # out-of-band file links
        self.register_plugin("xep_0085")           # chat states / typing
        # Delivery receipts: ack every incoming message that asks for one (the
        # operator's client then shows their message as delivered/read) and ask
        # for one on our own replies. Rooms are excluded by the plugin itself.
        self.register_plugin("xep_0184", pconfig={"auto_ack": True, "auto_request": True})
        self.register_plugin("xep_0280")           # message carbons (other resources)
        self.register_plugin("xep_0444")           # message reactions
        # Answer server pings and keep the stream warm; without this Prosody
        # drops an idle client after a few minutes.
        self.register_plugin("xep_0199", pconfig={"keepalive": True, "interval": 60, "timeout": 30})
        if cfg.features.uploads:
            # xep_0363 is not in slixmpp's default registry (its module exports the
            # class as XEP_0363, which load_plugin() does not look for), so register it.
            from slixmpp.plugins.base import register_plugin as register_plugin_class
            from slixmpp.plugins.xep_0363 import XEP_0363
            register_plugin_class(XEP_0363)
            self.register_plugin("xep_0363")       # HTTP file upload
        if cfg.features.omemo:
            omemo.install(self, self._omemo_storage_path())

        self.add_event_handler("session_start", self._on_session_start)
        # slixmpp's "message" event only fires for stanzas carrying <body>, so
        # OMEMO messages (which have <encrypted> and no body) would be dropped.
        # Match every message stanza instead and filter ourselves.
        from slixmpp.xmlstream.handler import Callback
        from slixmpp.xmlstream.matcher import MatchXPath
        self.register_handler(Callback(
            "hermes-messages",
            MatchXPath("{%s}message" % self.default_ns),
            self._on_message,
        ))
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self.add_event_handler("presence_subscribe", self._on_subscribe)
        self.add_event_handler("carbon_received", self._on_carbon_received)
        self.add_event_handler("omemo_initialized", self._on_omemo_ready)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("stream_error", self._on_stream_error)
        self.add_event_handler("connection_failed", self._on_connection_failed)
        self.add_event_handler("failed_auth", self._on_failed_auth)

    def _omemo_storage_path(self) -> str:
        """One OMEMO store per account: switching JIDs must not reuse identity state."""
        configured = Path(self.cfg.features.omemo_storage).expanduser()
        node = bare_jid(self.cfg.account.jid).split("@", 1)[0]
        return str(configured.with_name(f"{configured.stem}-{node}{configured.suffix}"))

    # -- lifecycle ----------------------------------------------------------

    def _on_session_start(self, _event) -> None:
        self.send_presence(pstatus="Hermes bridge")
        log.info("connected as %s", self.boundjid.bare)
        asyncio.create_task(self._after_start())

    async def _after_start(self) -> None:
        # Advertise presence to every contact that is subscribed to us; without a
        # direct presence after a fresh approval, clients can keep showing "offline".
        try:
            await self.get_roster()
        except Exception:  # noqa: BLE001
            log.exception("could not fetch roster")
        asyncio.create_task(self._presence_loop())
        asyncio.create_task(self._reaction_outbox_loop())
        for jid in list(self.client_roster.keys()):
            try:
                item = self.client_roster[jid]
                if item["subscription"] in ("from", "both"):
                    self.send_presence(pto=jid)
            except Exception:  # noqa: BLE001
                log.debug("presence to %s failed", jid, exc_info=True)

        if self.cfg.features.uploads:
            await self._discover_upload_service()
        node = bare_jid(self.cfg.account.jid).split("@", 1)[0]
        for room in self.cfg.rooms.join:
            for nick in dict.fromkeys((self.cfg.rooms.nick, node, f"{node}-bridge")):
                try:
                    await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
                    log.info("joined room %s as %s", room, nick)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("room %s as %s failed (%s); trying another nick", room, nick, exc)

    async def _presence_loop(self) -> None:
        """Re-advertise presence to subscribed contacts every few minutes.

        Some servers do not re-broadcast presence after a subscription is
        approved while the client was offline; contacts then sit at "offline".
        """
        while True:
            await asyncio.sleep(240)
            try:
                for jid in list(self.client_roster.keys()):
                    if self.client_roster[jid]["subscription"] in ("from", "both"):
                        self.send_presence(pto=jid)
            except Exception:  # noqa: BLE001
                log.debug("presence refresh failed", exc_info=True)

    def _on_carbon_received(self, msg) -> None:
        forwarded = msg["carbon_received"]
        log.info("carbon copy of a message addressed to another resource")
        self._on_message(forwarded)

    def _on_omemo_ready(self, _event) -> None:
        asyncio.create_task(self._log_omemo_identity())

    async def _log_omemo_identity(self) -> None:
        try:
            plugin = self.plugin["xep_0384"]
            session_manager = await plugin.get_session_manager()
            device, _others = await session_manager.get_own_device_information()
            log.info("OMEMO ready: device id %s, fingerprint %s",
                     device.device_id, device.identity_key.hex()[:16])
        except Exception:  # noqa: BLE001
            log.exception("could not read own OMEMO device information")

    async def _discover_upload_service(self) -> None:
        plugin = self.plugin["xep_0363"]
        configured = self.cfg.features.upload_service
        try:
            if configured:
                plugin.upload_service = JID(configured)
                self._upload_host = configured
                log.info("upload service set to %s", configured)
                return
            domain = JID(self.cfg.account.jid).domain
            info = await plugin.find_upload_service(domain=JID(domain))
            if info is None:
                log.warning("no HTTP upload service found on %s", domain)
                return
            self._upload_host = str(info["from"]).split("/")[0]
            log.info("discovered upload service %s", self._upload_host)
        except Exception:  # noqa: BLE001
            log.exception("HTTP upload discovery failed")

    def _on_disconnected(self, _event) -> None:
        log.warning("disconnected from XMPP")
        self._stopped.set()

    def _on_stream_error(self, error) -> None:
        log.error("XMPP stream error: %s", error)

    def _on_connection_failed(self, event) -> None:
        log.error("XMPP connection failed: %s", event)

    def _on_failed_auth(self, _event) -> None:
        log.error("XMPP authentication failed for %s", self.bot_bare)
        self._stopped.set()

    async def run_forever(self) -> None:
        self.connect(host=self.cfg.account.host, port=self.cfg.account.port)
        await self._stopped.wait()

    # -- subscriptions ------------------------------------------------------

    def _accept_subscription(self, jid: str) -> None:
        """Authorize the contact and request their presence back."""
        self.send_presence_subscription(pto=jid, ptype="subscribed")
        self.send_presence_subscription(pto=jid, ptype="subscribe")
        self.send_presence(pto=jid)

    def _on_subscribe(self, presence) -> None:
        """Owners and auto-accept are immediate; anyone else waits for an owner decision."""
        jid = presence["from"].bare
        owner_jids = {o.strip().lower() for o in self.cfg.policy.owners}
        if jid.strip().lower() in owner_jids or self.cfg.features.auto_accept_subscriptions:
            log.info("auto-accepting subscription from %s", jid)
            self._accept_subscription(jid)
            return
        new = self._pending.add(jid, str(presence["from"]))
        log.info("contact request from %s (%s) - notified owners", jid,
                 "new" if new else "already pending")
        asyncio.create_task(self._notify_owners_of_request(jid, str(presence["from"])))

    async def _notify_owners(self, text: str) -> None:
        for owner in self.cfg.policy.owners:
            await self._notify(owner, text)

    async def _notify_owners_of_request(self, jid: str, sender: str) -> None:
        when = time.strftime("%Y-%m-%d %H:%M")
        text = (f"Contact request: {jid} wants to add {self.bot_bare} as a contact "
                f"(requested {when}; seen from {sender}).\n"
                f"Approve: approve {jid}\nDecline: deny {jid}\nList: pending")
        for owner in self.cfg.policy.owners:
            await self._notify(owner, text)

    async def _notify(self, to: str, text: str) -> None:
        """Encrypted when the contact has devices, plaintext otherwise."""
        if self.cfg.features.omemo and await self._send_encrypted(to, text):
            return
        self._send(to, text)

    # -- inbound: 1:1 -------------------------------------------------------

    def _on_message(self, msg) -> None:
        # An invitation has no body: catch it here, before the body checks below,
        # so the bridge never depends on a plugin event being wired correctly.
        invite = room_invite(msg.xml)
        if invite is not None:
            asyncio.create_task(self._handle_room_invite(*invite))
            return
        raw_type = msg["type"]
        # Message text is never logged: the bridge's logs are not a transcript store.
        log.debug("handler saw message type=%s from=%s", raw_type, msg["from"])
        if raw_type not in ("chat", "normal"):
            log.debug("ignoring message type %s", raw_type)
            return
        # Carbon wrappers are handled by the carbon event, not directly.
        # (Checking msg["carbon_received"] is unreliable: it returns an empty
        # element object instead of None for ordinary messages.)
        carbon_ns = "{urn:xmpp:carbons:2}"
        if msg.xml.find(carbon_ns + "received") is not None or msg.xml.find(carbon_ns + "sent") is not None:
            return
        raw_body = msg["body"] or ""
        encrypted = bool(self.plugin["xep_0384"].is_encrypted(msg)) if self.cfg.features.omemo else False
        if not encrypted and not raw_body:
            return  # chat-state / receipt noise
        log.info("inbound <message> type=%s from=%s body_len=%d encrypted=%s",
                 raw_type, msg["from"], len(raw_body), encrypted)
        sender = bare_jid(str(msg["from"]))
        if sender == self.bot_bare:
            return  # never answer ourselves
        if is_ignored(sender, self.cfg.policy.ignore_jids):
            log.debug("ignoring message from %s (do-not-answer list)", sender)
            return
        if self.cfg.policy.quiet_unknown_senders and tier_for(sender, self.cfg.policy.owners) == GUEST:
            return
        if not self.limiter.allow(sender):
            log.warning("rate limited %s", sender)
            self.send_message(mto=sender, mbody="Slow down a moment — I'm still working through "
                                                "your last messages.", mtype="chat")
            return

        # The sender's stanza id is what a reaction has to address later; OMEMO
        # keeps it on the outer stanza, so read it before any decryption.
        stanza_id = str(msg["id"] or "")

        if encrypted:
            asyncio.create_task(self._handle_encrypted(sender, msg, stanza_id))
            return

        body = (msg["body"] or "").strip()
        if not body:
            return
        log.info("message from %s (%d chars, tier=%s)", sender, len(body),
                 tier_for(sender, self.cfg.policy.owners))
        self._dispatch(sender, body, encrypted=False, msg_id=stanza_id)

    async def _handle_encrypted(self, sender: str, msg, stanza_id: str = "") -> None:
        try:
            body, device = await omemo.decrypt_one_to_one(self, msg)
        except Exception as exc:  # noqa: BLE001 - not for us, or malformed
            log.warning("could not decrypt message from %s: %s", sender, exc)
            return
        body = (body or "").strip()
        if not body:
            return
        log.info("decrypted message from %s (device %s)", sender, device.device_id)
        self._dispatch(sender, body, encrypted=True, msg_id=stanza_id)

    # -- inbound: rooms -----------------------------------------------------

    def _on_groupchat(self, msg) -> None:
        room = str(msg["from"].bare)
        nick = str(msg["from"].resource)
        if nick == self.cfg.rooms.nick or nick == self.boundjid.resource:
            return
        # OMEMO in a room (XEP-0450) leaves only a fallback sentence as the body,
        # so a mention can never match it: decrypt before looking at the text.
        if self.cfg.features.omemo and self.plugin["xep_0384"].is_encrypted(msg):
            asyncio.create_task(self._handle_encrypted_groupchat(room, nick, msg))
            return
        body = (msg["body"] or "").strip()
        if not body:
            log.debug("groupchat message in %s from %s without a body", room, nick)
            return
        self._groupchat_body(room, nick, body, msg)

    async def _handle_encrypted_groupchat(self, room: str, nick: str, msg) -> None:
        """Read a room message that arrived OMEMO-encrypted (legacy or OMEMO 2)."""
        try:
            body = await omemo.decrypt_group(self, msg)
        except Exception as exc:  # noqa: BLE001 - device not in the room's list, not ours
            log.warning("could not decrypt an OMEMO room message from %s in %s: %s",
                        nick, room, exc)
            await self._warn_room_encryption(room)
            return
        body = (body or "").strip()
        if not body:
            return
        log.info("decrypted room message in %s from %s (%d chars)", room, nick, len(body))
        self._groupchat_body(room, nick, body, msg)

    async def _warn_room_encryption(self, room: str) -> None:
        """Say once an hour that a room's OMEMO excludes this account's device."""
        now = time.time()
        if now - self._room_warned.get(room, 0.0) < 3600:
            return
        self._room_warned[room] = now
        await self._notify_owners(
            f"Messages in {room} are OMEMO-encrypted and this account's device is not in "
            f"that room's device list, so they cannot be read. Either turn OMEMO off for "
            f"the room in your client, or add this device to it (its id is in the startup "
            f"line 'OMEMO ready: device id ...').")

    def _groupchat_body(self, room: str, nick: str, body: str, msg) -> None:
        addressed, text = self._room_addressed(body)
        # Every groupchat message is logged (length only): a silent room is then
        # distinguishable from a message that arrived but was not addressed to us.
        log.info("groupchat in %s from %s: addressed=%s, %d chars",
                 room, nick, addressed, len(body))
        if not addressed:
            return
        try:
            real_jid = str(msg["muc"]["item"]["jid"] or "")
        except Exception:  # noqa: BLE001 - anonymous room or no item element
            real_jid = ""
        tier = self._room_tier(nick, real_jid)
        if real_jid and is_ignored(real_jid, self.cfg.policy.ignore_jids):
            return
        key = f"{room}/{nick}"
        if not self.limiter.allow(key):
            return
        log.info("room message in %s from %s (tier=%s)", room, nick, tier)
        self._dispatch(key, text, encrypted=False, room=room, nick=nick, tier=tier,
                       real_jid=real_jid, msg_id=str(msg["id"] or ""))

    async def _handle_room_invite(self, room: str, inviter: str) -> None:
        """Owner invites are accepted; anyone else's becomes a question for the owners."""
        if not room:
            return
        log.info("room invite to %s from %s", room, inviter)
        if not self.cfg.rooms.accept_invites:
            await self._notify_owners(
                f"{inviter or 'Someone'} invited {self.bot_bare} to {room}. "
                f"Room invites are off; add the room to [rooms] join to answer there.")
            return
        if not inviter or tier_for(inviter, self.cfg.policy.owners) != OWNER:
            await self._notify_owners(
                f"Room invite: {inviter or 'unknown'} invited {self.bot_bare} to {room}. "
                f"Not an owner, so it was not joined — add it to [rooms] join to accept.")
            return
        nick = self.cfg.rooms.nick or "hermes"
        try:
            await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
        except Exception as exc:  # noqa: BLE001 - bans, nicks in use, room gone
            log.warning("join of invited room %s as %s failed: %s", room, nick, exc)
            await self._notify(inviter, f"I could not join {room} as {nick}: {exc}")
            return
        log.info("joined room %s on %s's invite", room, inviter)
        await self._notify(inviter, f"Joined {room} as {nick}. Address me as \"{nick}: ...\" there.")

    def _room_addressed(self, body: str) -> tuple[bool, str]:
        """Whether a room message is aimed at us, and the text minus the address.

        People type the nick, the JID's local part, or a client-side mention of
        whichever name they saw in the occupant list, so all of them count.
        """
        lowered = body.lower()
        names = [self.cfg.rooms.nick.lower()]
        account = getattr(self.cfg, "account", None)
        jid = str(getattr(account, "jid", "") or "")
        node = bare_jid(jid).split("@", 1)[0].lower() if jid else ""
        for name in (node, "hermes"):
            if name not in names:
                names.append(name)
        for name in names:
            for prefix in (f"{name}:", f"{name},", f"@{name}"):
                if lowered.startswith(prefix):
                    return True, body[len(prefix):].strip()
        # A mention can sit anywhere and carries punctuation: "@bot,", "bot?" and
        # a client-inserted "@name" all have to count. Compare bare words.
        words = body.split()
        if any(_mention_word(word) in names for word in words):
            rest = " ".join(w for w in words if _mention_word(w) not in names).strip(" ,:;")
            # A bare "ollama?" leaves nothing behind, and an empty prompt is
            # rejected by the runs API (HTTP 400) instead of answered: keep the
            # original text so the model sees what was actually said.
            return True, rest or body
        return self.cfg.rooms.respond == "always", body

    def _room_tier(self, nick: str, real_jid: str) -> str:
        if real_jid:
            try:
                if tier_for(real_jid, self.cfg.policy.owners) == OWNER:
                    return OWNER
            except ValueError:
                pass
        if self.cfg.rooms.trust_nicks and nick in self.cfg.rooms.owner_nicks:
            return OWNER
        return GUEST

    # -- turn plumbing ------------------------------------------------------

    def _dispatch(self, key: str, body: str, encrypted: bool, room: str | None = None,
                  nick: str | None = None, tier: str | None = None, real_jid: str = "",
                  msg_id: str = "") -> None:
        # Route one inbound message: commands first, then the per-chat runner."""
        text = (body or "").strip()
        item = {"body": text, "encrypted": encrypted, "room": room,
                "nick": nick, "tier": tier, "real_jid": real_jid, "id": msg_id}
        if room is None and tier is None and tier_for(key, self.cfg.policy.owners) == OWNER:
            command = approvals.parse_command(text)
            if command is not None:
                asyncio.create_task(self._handle_approval(key, *command))
                return
            invite = approvals.invite_command(text)
            if invite is not None:
                asyncio.create_task(self._handle_invite(key, item, key, invite))
                return
            room_to_join = approvals.join_command(text)
            if room_to_join is not None:
                asyncio.create_task(self._handle_join(key, item, key, room_to_join))
                return
        chat = self._chats.setdefault(key, _Chat())
        if _is_stop_command(text):
            self._stop_chat(key, chat, item)
            return
        chat.pending.append(item)
        if chat.task is None or chat.task.done():
            chat.task = asyncio.create_task(self._run_chat(key, chat))
        elif self.cfg.policy.on_new_message == "queue" or chat.media_turn:
            # Queue mode, or a turn that carries a photo: never throw media away
            # by interrupting. This message is answered as soon as it settles.
            log.info("queued a message for %s behind the running turn", key)
        else:
            # The model is still working on the previous message: interrupt it
            # now; the new text becomes the next turn as soon as it settles.
            log.info("interrupting the running turn for %s (new message)", key)
            self._interrupt(chat)

    def _interrupt(self, chat: _Chat) -> None:
        """Drop the current answer and ask the server to stop that run."""
        chat.suppress = True
        if not chat.run_id or chat.interrupt_requested or chat.endpoint is None:
            return
        chat.interrupt_requested = True
        endpoint, api_key, run_id = chat.endpoint, chat.api_key, chat.run_id
        asyncio.create_task(asyncio.to_thread(hermes_api.stop, endpoint, api_key, run_id))

    def _stop_chat(self, key: str, chat: _Chat, item: dict) -> None:
        """``stop``: forget queued text and interrupt the run in flight."""
        chat.pending.clear()
        chat.suppress = True
        self._interrupt(chat)
        log.info("stop requested by %s", key)
        asyncio.create_task(self._reply(key, item, "Stopped."))

    async def _handle_approval(self, owner: str, action: str, target: str) -> None:
        """Owner said approve/deny/pending in chat. Nothing here reaches the model."""
        if action == "list":
            await self._notify(owner, self._pending.describe())
            return
        if not target:
            if len(self._pending) == 1:
                target = next(iter(self._pending.all()))
            else:
                await self._notify(owner, "Which contact? " + self._pending.describe())
                return
        if action == "approve":
            if not self._pending.remove(target):
                await self._notify(owner, f"No pending request from {target}.")
                return
            self._accept_subscription(target)
            log.info("owner %s approved contact %s", owner, target)
            await self._notify(owner, f"Approved {target}. They can now exchange presence; "
                                      f"their messages still use the guest tier.")
        else:
            if not self._pending.remove(target):
                await self._notify(owner, f"No pending request from {target}.")
                return
            self.send_presence_subscription(pto=target, ptype="unsubscribed")
            log.info("owner %s denied contact %s", owner, target)
            await self._notify(owner, f"Declined {target}.")

    async def _handle_invite(self, key: str, item: dict, owner: str, target: str) -> None:
        """``invite <jid>``: send the presence subscription the bridge owns.

        A forced command — nothing here reaches the model, so it works even when
        the account's model cannot call tools.
        """
        if not target:
            await self._reply(key, item, "Usage: invite user@example.org")
            return
        try:
            parsed = JID(target)
        except Exception:  # noqa: BLE001 - malformed JID from chat
            parsed = None
        if "@" not in target or parsed is None or not parsed.node or not parsed.domain:
            await self._reply(key, item, f"{target} is not a JID. Usage: invite user@example.org")
            return
        jid = bare_jid(target)
        if jid == self.bot_bare:
            await self._reply(key, item, "That JID is me.")
            return
        if is_ignored(jid, self.cfg.policy.ignore_jids):
            await self._reply(key, item, f"{jid} is on the do-not-contact list.")
            return
        try:
            # Add the contact to the roster so the account keeps it after a restart.
            self.update_roster(jid, name=str(parsed.node))
        except Exception:  # noqa: BLE001 - the subscription request still counts
            log.debug("could not add %s to the roster", jid, exc_info=True)
        self.send_presence_subscription(pto=jid, ptype="subscribe")
        self.send_presence(pto=jid)
        log.info("owner %s invited %s", owner, jid)
        await self._reply(
            key, item,
            f"Invite sent to {jid}. They approve it in their client; they stay guest "
            f"tier unless they are in [policy] owners.")

    async def _handle_join(self, key: str, item: dict, owner: str, target: str) -> None:
        """``join <room>``: an owner asks the bridge into a room, no model involved."""
        if not target:
            await self._reply(key, item, "Usage: join room@conference.example.org")
            return
        try:
            parsed = JID(target)
        except Exception:  # noqa: BLE001 - malformed JID from chat
            parsed = None
        if "@" not in target or parsed is None or not parsed.node or not parsed.domain:
            await self._reply(key, item,
                              f"{target} is not a room JID. Usage: join room@conference.example.org")
            return
        room = str(parsed.bare)
        nick = self.cfg.rooms.nick or "hermes"
        try:
            await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
        except Exception as exc:  # noqa: BLE001 - bans, nick taken, room gone
            log.warning("join of %s as %s failed: %s", room, nick, exc)
            await self._reply(key, item, f"I could not join {room} as {nick}: {exc}")
            return
        log.info("owner %s asked me into %s (joined as %s)", owner, room, nick)
        await self._reply(
            key, item,
            f"Joined {room} as {nick}. Address me there as \"{nick}: ...\". Add it to "
            f"[rooms] join in this instance's hermes.toml to keep it after a restart.")

    async def _run_chat(self, key: str, chat: _Chat) -> None:
        """One runner per conversation: never two answers in flight for a user."""
        while chat.pending:
            items = chat.pending
            chat.pending = []
            item = self._merge_items(items)
            chat.suppress = False
            chat.interrupt_requested = False
            try:
                await self._handle_turn(key, chat, item)
            except Exception:  # noqa: BLE001 - one bad turn must not kill the chat
                log.exception("turn failed for %s", key)
                if not chat.suppress:
                    try:
                        await self._reply(key, item,
                                          "Something went wrong on my side. Try again in a moment.")
                    except Exception:  # noqa: BLE001
                        log.exception("could not report failure to %s", key)
            finally:
                chat.run_id = None

    @staticmethod
    def _merge_items(items: list[dict]) -> dict:
        """Fold messages that arrived during a run into one turn."""
        if len(items) == 1:
            return items[0]
        merged = dict(items[-1])
        merged["body"] = "\n\n".join(i["body"] for i in items if i.get("body"))
        merged["encrypted"] = any(i.get("encrypted") for i in items)
        return merged

    def _start_run(self, endpoint: Endpoint, api_key: str, turn: hermes_api.Turn,
                   sid: str, sender: str) -> str:
        """Admit a run; rotate the session id once if the server retired it.

        Deleting or pruning a session leaves a tombstone behind and Hermes then
        answers ``409 not_found`` for that id forever, so a derived id must be
        replaceable at runtime. The replacement still belongs to this one person
        and is reused for the rest of the process' lifetime.
        """
        try:
            return hermes_api.start(endpoint, api_key, turn, sid)
        except hermes_api.SessionRetired:
            fresh = f"{session_id_for(self.cfg.account.jid, sender)}-{secrets.token_hex(4)}"
            self._session_ids[sender] = fresh
            log.warning("session id for %s was retired; continuing in a fresh session", sender)
            return hermes_api.start(endpoint, api_key, turn, fresh)

    async def _handle_turn(self, key: str, chat: _Chat, item: dict) -> None:
        room = item.get("room")
        if room:
            sender = item.get("real_jid") or f"{key.split('/')[0]}/{item.get('nick')}"
            tier = item.get("tier") or GUEST
        else:
            sender = key
            tier = tier_for(sender, self.cfg.policy.owners)

        endpoint = self.cfg.owner if tier == OWNER else self.cfg.guest
        api_key = self.owner_key if tier == OWNER else self.guest_key
        system = (self.cfg.policy.owner_system_prompt if tier == OWNER
                  else self.cfg.policy.guest_system_prompt) or (
            DEFAULT_OWNER_PROMPT if tier == OWNER else DEFAULT_GUEST_PROMPT)
        who = f"{item.get('nick')} in the group chat {room}" if room else sender
        system = (f"{system}\n\nYou are the XMPP account {self.cfg.account.jid} "
                  f"(local part: {bare_jid(self.cfg.account.jid).split('@', 1)[0]}) "
                  f"and you are replying to {who}.")

        body = item["body"]
        notes: list[str] = []
        images: list[str] = []
        saved_paths: list[Path] = []
        if self.cfg.features.uploads and self._upload_host:
            for url in uploads.find_links(body, self._upload_host):
                try:
                    saved = await uploads.download(url, self._spool / bare_jid(sender),
                                                   self.cfg.features.max_upload_mb)
                    saved_paths.append(saved)
                    log.info("downloaded attachment from %s to %s", sender, saved)
                except Exception as exc:  # noqa: BLE001
                    log.warning("attachment download failed: %s", exc)
                    notes.append(f"[attachment {url} could not be downloaded]")
                    continue
                data_url = (inline_image(saved, self.cfg.features.max_inline_image_mb)
                            if self.cfg.features.inline_images else None)
                if data_url:
                    images.append(data_url)
                    # Guests get no host paths: their model has no tools to use them.
                    notes.append(f"[image attached: {saved.name}]" if tier == OWNER
                                 else "[image attached]")
                elif tier == OWNER:
                    notes.append(f"[attachment downloaded to {saved}]")
                else:
                    notes.append("[attachment received]")
        if notes:
            body = body + "\n\n" + "\n".join(notes)

        # An attachment must not be thrown away by a follow-up message.
        chat.media_turn = bool(saved_paths)
        skey = session_key_for(self.cfg.account.jid, sender)
        turn = hermes_api.Turn(system=system, user=body, session_key=skey,
                               images=tuple(images[:MAX_IMAGES_PER_TURN]))
        keepalive: asyncio.Task | None = None
        if not room:
            self._chat_state(key, "composing")
            # Chat states expire client-side (~30s), so refresh while the turn runs.
            keepalive = asyncio.create_task(self._composing_keepalive(key))
        # One session id per (bot, person), derived — never remembered on disk —
        # so every device of a user shares one transcript and nothing can fork it.
        sid = self._session_ids.get(sender) or session_id_for(self.cfg.account.jid, sender)
        try:
            try:
                if turn.images:
                    # The Runs API takes text only; inline images stay on the
                    # completions route (same deterministic session id).
                    answer = await asyncio.to_thread(
                        hermes_api.ask_with_images, endpoint, api_key, turn, sid)
                else:
                    run_id = await asyncio.to_thread(
                        self._start_run, endpoint, api_key, turn, sid, sender)
                    chat.endpoint, chat.api_key, chat.run_id = endpoint, api_key, run_id
                    # Live commentary ("hold on, let me look into that") is sent as
                    # the run produces it; the final answer still comes from wait().
                    interim: asyncio.Task | None = None
                    if self.cfg.features.interim_messages and not room:
                        interim = asyncio.create_task(
                            self._forward_interim(endpoint, api_key, run_id, key, item))
                    try:
                        answer = await asyncio.to_thread(
                            hermes_api.wait, endpoint, api_key, run_id, session_key=skey)
                    finally:
                        if interim is not None:
                            interim.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await interim
            except hermes_api.BridgeError as exc:
                # ``exc.detail`` can quote the prompt or the provider error body,
                # so only the fixed user-facing line reaches the log.
                log.error("backend error for %s: %s", key, exc.user_message)
                if not chat.suppress:
                    await self._reply(key, item, exc.user_message)
                return
            finally:
                chat.run_id = None

            if chat.suppress or answer.cancelled:
                log.info("answer for %s discarded (interrupted)", key)
                return
            reply = answer.text
            if not reply.strip():
                return

            clean, files = uploads.extract_markers(reply)
            # A reaction is sent before the text: it is the acknowledgement, and
            # it must not wait behind an upload or a second chunk.
            clean, emoji = reactions.extract_markers(clean)
            if emoji:
                await self._send_reaction(item.get("id") or "", emoji,
                                          to=room or key,
                                          mtype="groupchat" if room else "chat")
                if not clean and not files:
                    log.info("reaction-only reply for %s", key)
                    return
            attachment_urls: list[str] = []
            if files and self.cfg.features.uploads and tier == OWNER:
                for path in files:
                    try:
                        attachment_urls.append(
                            await uploads.upload(self.plugin["xep_0363"], path,
                                                 self.cfg.features.max_upload_mb))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("upload of %s failed: %s", path, exc)
                        clean += f"\n(could not upload {path.name}: {exc})"

            log.info("reply for %s ready: %d chars, %d attachment(s), %d chunk(s)",
                     key, len(clean), len(attachment_urls), len(split_message(clean, self.cfg.policy.max_chunk_chars)))
            for chunk in split_message(clean, self.cfg.policy.max_chunk_chars):
                if chat.suppress:
                    log.info("stopped sending to %s (interrupted mid-reply)", key)
                    return
                await self._reply(key, item, chunk)
                await asyncio.sleep(0.2)
            for url in attachment_urls:
                self._send_attachment(key, item, url)
        finally:
            chat.media_turn = False
            if keepalive is not None:
                keepalive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keepalive
            if not room and not chat.suppress:
                self._chat_state(key, "active")
            # Nothing the contact sent stays on disk: the spool is not an archive.
            for path in saved_paths:
                with contextlib.suppress(OSError):
                    path.unlink()

    # -- outbound -----------------------------------------------------------

    async def _forward_interim(self, endpoint: Endpoint, api_key: str, run_id: str,
                               key: str, item: dict) -> None:
        """Send the model's mid-turn commentary as it happens.

        Hermes emits ``message.interim`` on the run's event stream when the model
        talks before finishing a tool call. Forwarding it turns "let me look that
        up" into a real message instead of burying it at the end of the turn.
        """
        import threading

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stop = threading.Event()

        def on_event(event: dict) -> None:
            if event.get("event") != hermes_api.INTERIM_EVENT:
                return
            text = str(event.get("text") or "").strip()
            if text:
                loop.call_soon_threadsafe(queue.put_nowait, text)

        worker = asyncio.create_task(asyncio.to_thread(
            hermes_api.stream_run_events, endpoint, api_key, run_id, on_event, stop))
        worker.add_done_callback(lambda _task: loop.call_soon_threadsafe(queue.put_nowait, None))
        seen: set[str] = set()
        sent = 0
        limit = self.cfg.features.max_interim_messages
        try:
            while sent < limit:
                text = await queue.get()
                if text is None:
                    return
                if text in seen or self._chats[key].suppress:
                    continue
                seen.add(text)
                sent += 1
                log.info("interim message for %s (%d chars)", key, len(text))
                for chunk in split_message(text, self.cfg.policy.max_chunk_chars):
                    await self._reply(key, item, chunk)
        finally:
            stop.set()
            worker.cancel()

    async def _reply(self, key: str, item: dict, text: str) -> None:
        room = item.get("room")
        if room:
            await self._send_room(item, text)
            return
        if item.get("encrypted") and self.cfg.features.omemo:
            if await self._send_encrypted(key, text):
                return
            mode = self.cfg.features.omemo_mode
            hint = "" if mode == "auto" else f" This bridge is set to OMEMO {mode} only."
            self.send_message(mto=key, mtype="chat",
                              mbody=f"(I couldn't encrypt that reply for your device.{hint})")
            return
        self._send(key, text)

    async def _encrypt_room(self, room: str, text: str):
        """Build an OMEMO 2 groupchat stanza for ``room``, or raise."""
        return await asyncio.wait_for(
            omemo.encrypt_group(self, room, text, self._room_recipients(room),
                                self.bot_bare),
            timeout=45)

    async def _send_room(self, item: dict, text: str) -> None:
        """A reply in a room: encrypted with OMEMO 2 when that room asks for it.

        A room that is meant to be encrypted never gets a plaintext fallback: if the
        message cannot be encrypted (a member without an OMEMO device, an anonymous
        room with no member JIDs) the bridge says so in the room instead of leaking
        the content.
        """
        room = item["room"]
        if self.cfg.rooms.encrypt and self.cfg.features.omemo:
            stanza = None
            try:
                stanza = await self._encrypt_room(room, text)
            except Exception as exc:  # noqa: BLE001 - trust may just be missing
                log.warning("could not encrypt a room reply for %s: %s", room, exc)
                try:
                    # A member whose devices were only ever seen on the other
                    # OMEMO backend has no trusted device, which refuses the
                    # whole message; trusting what we know fixes that.
                    await omemo.trust_room_devices(self, self._room_recipients(room))
                    stanza = await self._encrypt_room(room, text)
                except Exception as retry:  # noqa: BLE001 - give up on encryption
                    log.warning("still cannot encrypt for %s after forcing trust: %s",
                                room, retry)
            if stanza is None:
                if not self.cfg.rooms.plaintext_fallback:
                    self.send_message(
                        mto=room, mtype="groupchat",
                        mbody="I can't encrypt to this room right now, so I won't send that. "
                              "Everyone here needs an OMEMO-capable client, and the room must "
                              "not hide member addresses.")
                    return
                # Explicitly opted in: an answer in the clear beats no answer.
                log.warning("sending an unencrypted reply to %s", room)
                self.send_message(mto=room, mtype="groupchat",
                                  mbody="[unencrypted] " + text)
                return
            stanza.send()
            log.info("sent encrypted room reply to %s (%d chars)", room, len(text))
            return
        self.send_message(mto=room, mbody=text, mtype="groupchat")

    def _room_recipients(self, room: str) -> set[str]:
        """Bare JIDs of everyone in the room (empty for anonymous rooms).

        XEP-0045 keeps ``rooms[pfrom][room][nick]``; pfrom is None unless the client
        uses multi-from addressing, so look through every bucket.
        """
        plugin = self.plugin["xep_0045"]
        roster: dict = {}
        for bucket in plugin.rooms.values():
            found = bucket.get(JID(room)) or bucket.get(room)
            if found:
                roster = found
                break
        out: set[str] = set()
        for nick in list(roster):
            try:
                jid = plugin.get_jid_property(JID(room), nick, "jid")
            except Exception:  # noqa: BLE001 - the nick left between the two calls
                jid = None
            if jid:
                out.add(str(JID(jid).bare))
        return out

    async def _send_encrypted(self, to: str, text: str, stanza=None,
                              what: str = "reply") -> bool:
        plugin = self.plugin["xep_0384"]
        mode = self.cfg.features.omemo_mode
        if mode in ("2", "legacy"):
            # Refuse a silent downgrade: if the contact's devices exist but none
            # speaks the pinned version, say so instead of encrypting with the other.
            wanted = OMEMO2_NAMESPACE if mode == "2" else LEGACY_NAMESPACE
            try:
                manager = await plugin.get_session_manager()
                devices = await manager.get_device_information(JID(to).bare)
            except Exception:  # noqa: BLE001 - an unreadable list must not wedge the reply
                devices = frozenset()
            if devices and not any(wanted in device.namespaces for device in devices):
                log.warning("omemo_mode=%s: %s has no device for %s", mode, to, wanted)
                return False
        try:
            if stanza is None:
                stanza = self.make_message(mto=to, mtype="chat", mbody=text)
            # A bundle fetch for a stale device must never wedge the conversation.
            encrypted, errors = await asyncio.wait_for(
                plugin.encrypt_message(stanza, JID(to), identifier=to), timeout=45)
            if encrypted is None:
                log.warning("nothing to encrypt for %s (errors=%s)", to, errors)
                return False
            encrypted.send()
            log.info("sent encrypted %s to %s (%d chars, %d device errors)",
                     what, to, len(text), len(errors))
            return True
        except asyncio.TimeoutError:
            log.error("OMEMO encryption for %s timed out after 45s", to)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("OMEMO encryption for %s failed: %s", to, exc)
            return False

    def _send(self, to: str, text: str) -> None:
        try:
            self.send_message(mto=to, mbody=text, mtype="chat")
            log.info("sent reply to %s (%d chars)", to, len(text))
        except Exception:  # noqa: BLE001
            log.exception("failed to send to %s", to)

    def _send_attachment(self, key: str, item: dict, url: str) -> None:
        try:
            msg = self.make_message(mto=item.get("room") or key,
                                    mtype="groupchat" if item.get("room") else "chat",
                                    mbody=url)
            msg["oob"]["url"] = url
            msg.send()
        except Exception:  # noqa: BLE001
            log.exception("failed to send attachment link %s", url)

    async def _send_reaction(self, target_id: str, emojis: list[str], *, to: str,
                             mtype: str = "chat") -> bool:
        """React to the message ``target_id`` (XEP-0444), encrypted when possible."""
        if not target_id:
            log.debug("no stanza id to react to for %s", to)
            return False
        if not emojis:
            return False
        try:
            msg = self.make_message(mto=to, mtype=mtype)
            self.plugin["xep_0444"].set_reactions(msg, target_id, emojis)
            msg.enable("store")  # keep the reaction in the contact's archive
        except Exception:  # noqa: BLE001 - a cosmetic feature must not break a turn
            log.exception("could not build a reaction for %s", to)
            return False
        if self.cfg.features.omemo and mtype == "chat":
            if await self._send_encrypted(to, "", stanza=msg, what="reaction"):
                return True
            mode = self.cfg.features.omemo_mode
            if mode != "auto":
                log.warning("reaction to %s dropped: omemo_mode=%s forbids a plaintext stanza",
                            to, mode)
                return False
            # Legacy OMEMO 0.3 encrypts the body only, and a reaction has none:
            # the stanza goes out in the clear rather than not at all.
            log.info("reaction to %s on id %s goes out unencrypted (no ciphertext for a "
                     "body-less stanza)", to, target_id)
        try:
            msg.send()
            log.info("sent reaction %s to %s on id %s", "".join(emojis), to, target_id)
            return True
        except Exception:  # noqa: BLE001
            log.exception("failed to send a reaction to %s", to)
            return False

    async def _reaction_outbox_loop(self, interval: float = 2.0) -> None:
        """Send reactions queued by the operator CLI (``hermes-xmpp react``).

        File-based on purpose: the running bridge owns the OMEMO sessions, so it
        has to be the one encrypting. A job is deleted only after it is handled.
        """
        await asyncio.sleep(5.0)  # let OMEMO finish loading its store first
        outbox = self._spool / reactions.OUTBOX_DIR
        while True:
            try:
                for path in sorted(outbox.glob("*.json")):
                    await self._drain_outbox(path)
            except Exception:  # noqa: BLE001 - the loop must survive a bad pass
                log.exception("reaction outbox pass failed")
            await asyncio.sleep(interval)

    async def _drain_outbox(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            to = bare_jid(str(data["to"]))
            text = data.get("text")
            if text:
                await self._notify(to, str(text))
                log.info("delivered queued message to %s", to)
                with contextlib.suppress(OSError):
                    path.unlink()
                return
            target_id = str(data["id"])
            emojis = [str(item) for item in data.get("reactions") or []]
        except Exception as exc:  # noqa: BLE001 - a poison job must not wedge the queue
            log.warning("discarding unreadable reaction job %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return
        await self._send_reaction(target_id, emojis, to=to)
        with contextlib.suppress(OSError):
            path.unlink()

    async def _composing_keepalive(self, to: str, interval: float = 15.0) -> None:
        """Re-send ``composing`` until cancelled: clients drop the state after ~30s."""
        while True:
            await asyncio.sleep(interval)
            self._chat_state(to, "composing")

    def _chat_state(self, to: str, state: str) -> None:
        try:
            msg = self.make_message(mto=to, mtype="chat")
            msg["chat_state"] = state
            msg.send()
        except Exception:  # noqa: BLE001 - cosmetic only
            log.debug("chat state %s failed", state, exc_info=True)
