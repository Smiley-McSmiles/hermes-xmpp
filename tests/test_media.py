import asyncio
import base64
import os
import urllib.error
from urllib.parse import urlparse

import pytest

from hermes_xmpp import uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


def _encrypt(blob, layout="xep0454"):
    """Build (ciphertext, fragment) the way clients do it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    if layout == "xep0454":            # iv(12) key(32) tag(16) in the fragment
        iv = os.urandom(12)
        sealed = AESGCM(key).encrypt(iv, blob, None)
        body, tag = sealed[:-16], sealed[-16:]
        material = iv + key + tag
    else:                              # Conversations: iv(12) key(32), tag in the file
        iv = os.urandom(12)
        body = AESGCM(key).encrypt(iv, blob, None)
        material = iv + key
    fragment = base64.urlsafe_b64encode(material).decode().rstrip("=")
    return body, fragment


def test_find_links_accepts_aesgcm_and_https_on_the_upload_host_only():
    text = (
        "see aesgcm://upload.example.org/abc#Zm9v and https://upload.example.org/x.jpg "
        "and aesgcm://evil.example/abc#Zm9v and http://upload.example.org/insecure"
    )
    links = uploads.find_links(text, "upload.example.org")
    assert links == ["aesgcm://upload.example.org/abc#Zm9v",
                     "https://upload.example.org/x.jpg"]


def test_aesgcm_key_reads_both_spellings():
    material = os.urandom(60)
    fragment = base64.urlsafe_b64encode(material).decode().rstrip("=")
    assert uploads.aesgcm_key(urlparse(f"aesgcm://h.example/a.png#{fragment}")) == material
    assert uploads.aesgcm_key(urlparse(f"https://h.example/a.png#aesgcm={fragment}")) == material


def test_aesgcm_key_accepts_a_hex_fragment():
    """ward sends hex (88 chars for iv+key), not base64url."""
    material = os.urandom(44)
    parsed = urlparse(f"aesgcm://h.example/a.png#{material.hex()}")
    assert uploads.aesgcm_key(parsed) == material


def test_aesgcm_key_is_none_for_plain_https():
    assert uploads.aesgcm_key(urlparse("https://h/a.png")) is None


def test_aesgcm_key_rejects_an_empty_fragment():
    with pytest.raises(ValueError):
        uploads.aesgcm_key(urlparse("aesgcm://h/a.png#"))


def test_download_decrypts_a_hex_fragment_from_ward(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, iv = os.urandom(32), os.urandom(12)
    body = AESGCM(key).encrypt(iv, PNG, None)  # tag rides at the end of the blob
    monkeypatch.setattr(uploads, "_fetch", lambda url, max_mb: body)
    path = uploads._download_blocking(f"aesgcm://upload.example.org/x#{(iv + key).hex()}",
                                      tmp_path, 10)
    assert path.read_bytes() == PNG


@pytest.mark.parametrize("layout", ["xep0454", "conversations"])
def test_download_decrypts_encrypted_media_and_names_it_by_content(tmp_path, monkeypatch, layout):
    body, fragment = _encrypt(PNG, layout)
    monkeypatch.setattr(uploads, "_fetch", lambda url, max_mb: body)
    url = f"aesgcm://upload.example.org/blob123#{fragment}"
    path = uploads._download_blocking(url, tmp_path, 10)
    assert path.name.endswith(".png")
    assert path.read_bytes() == PNG


def test_download_keeps_plain_https_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "_fetch", lambda url, max_mb: PNG)
    path = uploads._download_blocking("https://upload.example.org/x", tmp_path, 10)
    assert path.read_bytes() == PNG
    assert path.name.endswith(".png")


def test_download_retries_while_the_upload_is_still_running(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(url, dest, max_mb):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(url, 404, "not yet", {}, None)
        target = dest / "x.png"
        dest.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG)
        return target

    monkeypatch.setattr(uploads, "_download_blocking", flaky)
    monkeypatch.setattr(uploads, "DOWNLOAD_BACKOFF_S", (0.0, 0.0))
    path = asyncio.run(uploads.download("https://upload.example.org/x.png", tmp_path, 10))
    assert path.read_bytes() == PNG
    assert calls["n"] == 3


def test_download_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    def always_404(url, dest, max_mb):
        raise urllib.error.HTTPError(url, 404, "gone", None, None)

    monkeypatch.setattr(uploads, "_download_blocking", always_404)
    monkeypatch.setattr(uploads, "DOWNLOAD_BACKOFF_S", (0.0, 0.0))
    with pytest.raises(urllib.error.HTTPError):
        asyncio.run(uploads.download("https://upload.example.org/x.png", tmp_path, 10))


def test_sniff_extension_recognises_common_images():
    assert uploads.sniff_extension(PNG) == ".png"
    assert uploads.sniff_extension(b"\xff\xd8\xff\xe0rest") == ".jpg"
    assert uploads.sniff_extension(b"not an image") == ""
