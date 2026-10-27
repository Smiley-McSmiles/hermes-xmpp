"""Owner approval for inbound presence subscriptions.

With ``features.auto_accept_subscriptions = false`` the bridge never adds a stranger
on its own: each request is stored here and the owners are asked in chat. The owner
answers with ``approve <jid>``, ``deny <jid>`` or ``pending``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

APPROVE_WORDS = frozenset({"approve", "accept"})
DENY_WORDS = frozenset({"deny", "decline"})
LIST_WORDS = frozenset({"pending", "requests", "invites"})
INVITE_WORDS = frozenset({"invite"})
JOIN_WORDS = frozenset({"join"})


def join_command(text: str) -> str | None:
    """Room from ``join <room@conference.example.org>``, ``""`` without one, else None.

    Owner-only, like ``invite``: the bridge joins the room itself, so the bot can be
    taken into a room from a chat message without a model turn or a config edit.
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    if tokens[0].lower().rstrip(":,") not in JOIN_WORDS:
        return None
    return tokens[1].strip().rstrip(".,") if len(tokens) > 1 else ""


def invite_command(text: str) -> str | None:
    """Target from ``invite <jid>``, ``""`` when the verb has no target, else None.

    ``invite`` is a forced command: the bridge sends the presence subscription
    itself, so asking a new person to become a contact never costs a model turn
    and works even when the model cannot call tools.
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    if tokens[0].lower().rstrip(":,") not in INVITE_WORDS:
        return None
    return tokens[1].strip().rstrip(".,") if len(tokens) > 1 else ""


def parse_command(text: str) -> tuple[str, str] | None:
    """``("approve"|"deny"|"list", jid_or_empty)`` for an owner command, else None.

    Only the explicit verbs count; a bare "yes" stays a normal chat message so an
    ordinary conversation can never approve a stranger by accident.
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    verb = tokens[0].lower().rstrip(":,")
    if verb in APPROVE_WORDS or verb in DENY_WORDS:
        action = "approve" if verb in APPROVE_WORDS else "deny"
        target = tokens[1].lower().rstrip(".,") if len(tokens) > 1 else ""
        if target and "@" not in target:
            return None  # "deny that claim" is conversation, not a command
        return action, target
    if verb in LIST_WORDS and len(tokens) == 1:
        return "list", ""
    return None


class PendingStore:
    """JSON-file queue of contact requests awaiting an owner decision."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, jid: str, sender: str = "") -> bool:
        """Record a request; True when this JID was not already pending."""
        key = jid.strip().lower()
        new = key not in self._items
        self._items[key] = {"jid": key, "from": sender, "requested_at": time.time()}
        self._save()
        return new

    def remove(self, jid: str) -> bool:
        key = jid.strip().lower()
        existed = self._items.pop(key, None) is not None
        if existed:
            self._save()
        return existed

    def get(self, jid: str) -> dict | None:
        return self._items.get(jid.strip().lower())

    def all(self) -> dict[str, dict]:
        return dict(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def describe(self) -> str:
        """One line per pending request, for the owner's chat."""
        if not self._items:
            return "No pending contact requests."
        lines = []
        for entry in sorted(self._items.values(), key=lambda e: e["jid"]):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("requested_at", 0)))
            lines.append(f"{entry['jid']} — requested {when}")
        return "Pending contact requests:\n" + "\n".join(lines)
