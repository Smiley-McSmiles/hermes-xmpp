"""File transfer: XEP-0363 uploads out, HTTPS downloads in.

Outbound: the agent can point at local files with a marker in its reply

    Here is the log you asked for. [[attach: /home/owner/ward.log]]

and the bridge uploads each file to the account's HTTP upload service, then
sends the URL (+ OOB element) instead of the marker.

Inbound: links on the configured upload host are downloaded into a per-contact
spool directory so the agent can read them. Only that host is fetched — no
arbitrary URL from a chat message is ever requested (SSRF guard).

Encrypted media (OMEMO media sharing, XEP-0454) arrives as a URL plus key
material in the fragment, so the bytes on the server are AES-256-GCM
ciphertext. Both spellings are handled: the ``aesgcm://`` scheme with a raw
base64url fragment (current clients) and ``https://...#aesgcm=...`` (older
Conversations). A first GET can race the sender's upload, so downloads retry
with backoff instead of failing the turn.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hermes_xmpp.uploads")

MARKER_RE = re.compile(r"\[\[\s*attach:\s*(?P<path>[^\]\n]+?)\s*\]\]")
_URL_RE = re.compile(r"(?:https?|aesgcm)://[^\s<>\"']+")
_AESGCM_FRAGMENT_RE = re.compile(r"(?:^|#|&)aesgcm=([A-Za-z0-9+/=_-]+)")
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_S = (1.0, 3.0)
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


@dataclass(frozen=True)
class Attachment:
    path: Path
    url: str


def extract_markers(text: str) -> tuple[str, list[Path]]:
    """Split reply text into (text without markers, list of existing files)."""
    paths: list[Path] = []
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        candidate = Path(os.path.expanduser(match.group("path").strip()))
        if candidate.is_file():
            paths.append(candidate)
            return ""
        missing.append(str(candidate))
        return ""

    cleaned = MARKER_RE.sub(repl, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, paths


def find_links(text: str, host: str) -> list[str]:
    """Return media URLs on ``host`` (case-insensitive, exact host match).

    ``https`` links and ``aesgcm://`` encrypted-media links qualify; a plaintext
    http link never does.
    """
    if not host:
        return []
    links: list[str] = []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(").,;]")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("https", "aesgcm"):
            continue
        if (parsed.hostname or "").lower() != host.lower():
            continue
        links.append(url)
    return links


def aesgcm_key(parsed: urllib.parse.ParseResult) -> bytes | None:
    """Key material from an encrypted-media URL, or None when it is plain https."""
    fragment = parsed.fragment or ""
    if parsed.scheme != "aesgcm" and "aesgcm=" not in fragment:
        return None
    match = _AESGCM_FRAGMENT_RE.search("#" + fragment)
    payload = match.group(1) if match else fragment
    payload = payload.strip()
    if not payload:
        raise ValueError("encrypted media link has no key material")
    # Clients disagree on the encoding: hex (ward, 88 chars for iv+key) or
    # base64url (Conversations/Gajim). Hex is unambiguous when it is all hex
    # digits and an even length, since base64url would need a wider alphabet.
    if len(payload) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in payload):
        return bytes.fromhex(payload)
    padded = payload + "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"unreadable encrypted-media key: {exc}") from exc


def _decrypt_aesgcm(blob: bytes, material: bytes) -> bytes:
    """AES-256-GCM open, tolerating the layouts clients actually send.

    XEP-0454 is iv(12)|key(32)|tag(16); Conversations sends iv(12)|key(32) with
    the tag appended to the file; some clients use a 16-byte IV, and at least one
    (seen from Ward) pads the material to 66 bytes, which used to fail as
    ``unexpected encrypted-media key length 66`` and silently dropped the image.

    The GCM tag is the discriminator: try the standard splits first, then the same
    splits at every offset and with up to 8 trailing bytes dropped, and keep the
    first that authenticates. A wrong split cannot decrypt, so a match is proof.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    n = len(material)
    for trim in range(0, 9):
        body = material[: n - trim] if trim else material
        for start in range(0, max(0, len(body) - 44) + 1):
            for iv_len in (12, 16):
                end = start + iv_len + 32
                if end > len(body):
                    continue
                iv = body[start:start + iv_len]
                key = body[start + iv_len:end]
                tail = body[end:]
                for tag in (b"", tail[:16] if len(tail) >= 16 else b""):
                    try:
                        plain = AESGCM(key).decrypt(iv, blob + tag, None)
                    except Exception:  # noqa: BLE001 - wrong split, try the next one
                        continue
                    if start or trim or iv_len != 12 or tag != tail[:16]:
                        log.info("encrypted media: layout iv=%d offset=%d trim=%d tag=%s",
                                 iv_len, start, trim, "material" if tag else "file")
                    return plain
    raise ValueError(f"unexpected encrypted-media key length {len(material)}")


def sniff_extension(data: bytes) -> str:
    """Image extension from magic bytes; empty when it is not a known image."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def safe_name(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).name or "attachment"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:120] or "attachment"


async def upload(plugin, path: Path, max_mb: int) -> str:
    """Upload one file and return its HTTPS URL."""
    import asyncio

    size = path.stat().st_size
    if size > max_mb * 1024 * 1024:
        raise ValueError(f"{path.name} is larger than the {max_mb} MB limit")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return await plugin.upload_file(filename=path.name, size=size,
                                   content_type=content_type,
                                   input_file=path.open("rb"))


def _fetch(url: str, max_mb: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-xmpp/0.3"})
    limit = max_mb * 1024 * 1024
    chunks: list[bytes] = []
    written = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        while chunk := response.read(64 * 1024):
            written += len(chunk)
            if written > limit:
                raise ValueError(f"attachment exceeds {max_mb} MB")
            chunks.append(chunk)
    return b"".join(chunks)


def _download_blocking(url: str, dest_dir: Path, max_mb: int) -> Path:
    parsed = urllib.parse.urlparse(url)
    material = aesgcm_key(parsed)
    fetch_url = url
    if parsed.scheme == "aesgcm":
        # aesgcm://host/path#key: the server only ever speaks https.
        fetch_url = parsed._replace(scheme="https").geturl()
    blob = _fetch(fetch_url, max_mb)
    if material is not None:
        blob = _decrypt_aesgcm(blob, material)
    name = safe_name(url)
    ext = sniff_extension(blob)
    if ext and not name.lower().endswith(ext):
        name = f"{name.rsplit('.', 1)[0] if '.' in name else name}{ext}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(blob)
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return target


async def download(url: str, dest_dir: Path, max_mb: int) -> Path:
    """Fetch one attachment, retrying: the message can outrun the sender's upload."""
    import asyncio
    import urllib.error

    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            return await asyncio.to_thread(_download_blocking, url, dest_dir, max_mb)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (403, 404, 500, 502, 503):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < DOWNLOAD_ATTEMPTS:
            await asyncio.sleep(DOWNLOAD_BACKOFF_S[min(attempt, len(DOWNLOAD_BACKOFF_S) - 1)])
    assert last_error is not None
    raise last_error
