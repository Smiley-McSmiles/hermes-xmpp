"""Message reactions: XEP-0444 out, a marker in.

The agent asks for a reaction by putting a marker in its reply::

    Sounds good, shipping that now. [[react: 👍]]

The bridge strips the marker and sends a ``<reactions>`` stanza that addresses
the id of the message being answered, so the reaction lands on the question
instead of arriving as another line of chat. A marker may hold several emoji,
and several markers merge into one set (order kept, duplicates dropped, capped
at :data:`MAX_REACTIONS`).

Encryption is best-effort by protocol: OMEMO 2 / 0.8 encrypts the whole stanza
(SCE), so the reaction travels inside the ciphertext. Legacy OMEMO 0.3
(``eu.siacs.conversations.axolotl``) encrypts the body only, and a stanza
without a body has nothing to encrypt — there the reaction goes out in the
clear, which the calling code logs rather than hides.

Operator-triggered reactions use the same marker-free path: ``queue_reaction``
writes a job into the bridge's outbox and the running bridge sends it (it holds
the OMEMO sessions, so it must be the one doing the encrypting).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

MARKER_RE = re.compile(r"\[\[\s*react:\s*(?P<emoji>[^\[\]\n]{1,64}?)\s*\]\]")
MAX_REACTIONS = 4
OUTBOX_DIR = "outbox"


def extract_markers(text: str) -> tuple[str, list[str]]:
    """Split reply text into (text without markers, ordered emoji to send)."""
    if "[[" not in text:
        return text, []
    emojis: list[str] = []

    def repl(match: re.Match[str]) -> str:
        for token in match.group("emoji").split():
            if token and token not in emojis:
                emojis.append(token)
        return ""

    cleaned = MARKER_RE.sub(repl, text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, emojis[:MAX_REACTIONS]


def build_job(to: str, message_id: str, emojis: list[str]) -> dict[str, object]:
    """The JSON body of one outbox job (also the operator CLI's on-disk format)."""
    return {"to": to, "id": str(message_id), "reactions": list(emojis)[:MAX_REACTIONS]}


def queue_reaction(spool_dir: str | Path, to: str, message_id: str, emojis: list[str]) -> Path:
    """Hand a reaction to the running bridge; returns the job file it will pick up."""
    if not str(message_id).strip():
        raise ValueError("a reaction needs the id of the message it is for")
    if not emojis:
        raise ValueError("a reaction needs at least one emoji")
    return _write_job(spool_dir, build_job(to, message_id, emojis))


def queue_text(spool_dir: str | Path, to: str, text: str) -> Path:
    """Queue a chat message for the running bridge (encrypted like any reply).

    For the case where a turn's answer has nowhere to go — the bridge was
    restarted while the operator's request was still in flight.
    """
    if not text.strip():
        raise ValueError("a queued message needs text")
    return _write_job(spool_dir, {"to": to, "text": text})


def _write_job(spool_dir: str | Path, job: dict) -> Path:
    outbox = Path(spool_dir).expanduser() / OUTBOX_DIR
    outbox.mkdir(parents=True, exist_ok=True)
    # Time-ordered name: the bridge sends jobs in the order they were queued.
    target = outbox / f"{time.time_ns():020d}-{secrets.token_hex(4)}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(job) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(target)  # atomic: a half-written job is never read
    return target
