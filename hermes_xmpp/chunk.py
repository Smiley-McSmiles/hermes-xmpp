"""Split long agent replies into several XMPP messages.

XMPP clients render a 30 kB stanza badly and some servers throttle large
messages, so replies are cut on paragraph/line boundaries. Code fences are
re-opened (and closed) across chunks so a split never produces an unterminated
block in the reader's client.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _hard_slice(line: str, limit: int) -> list[str]:
    """Cut one over-long line at word boundaries (or exactly, if unbroken)."""
    out: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Return 1..n stanzas, each comfortably under ``limit`` characters."""
    if not text:
        return []
    text = text.replace("\r\n", "\n").rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence: str | None = None

    def emit() -> None:
        nonlocal current, current_len
        body = "\n".join(current).strip("\n")
        if fence and body:
            body = f"{body}\n{fence}"
        if body.strip():
            chunks.append(body)
        current = []
        current_len = 0

    for raw_line in text.split("\n"):
        for line in _hard_slice(raw_line, max(200, limit - 20)):
            reserve = len(fence) + 2 if fence else 0
            needed = len(line) + (1 if current else 0)
            if current and current_len + needed + reserve > limit:
                emit()
                if fence:
                    current.append(fence)
                    current_len = len(fence) + 1
                    needed = len(line) + 1
            current.append(line)
            current_len += needed

            match = _FENCE.match(line)
            if match:
                marker = match.group(1)
                if fence is None:
                    fence = marker
                elif line.strip() == fence.rstrip():
                    fence = None

    if current:
        emit()
    return [c for c in chunks if c.strip()]
