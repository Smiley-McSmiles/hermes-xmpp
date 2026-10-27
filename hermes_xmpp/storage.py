"""Persistent key/value storage for OMEMO state.

python-omemo keeps identity keys, sessions, device lists and trust decisions in
a ``Storage``; losing it means losing the ability to read earlier messages and
re-publishing a new identity. Writes are atomic (tmp + rename) and the file is
0600, since it contains long-term private key material.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from omemo.storage import Just, Maybe, Nothing, Storage


class JSONFileStorage(Storage):
    """A single JSON file holding the whole OMEMO store."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(os.path.expanduser(str(path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # A corrupt store is fatal for OMEMO; keep the evidence instead
                # of silently starting a new identity.
                raise
        self._flush()

    async def _load(self, key: str) -> Maybe[Any]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
