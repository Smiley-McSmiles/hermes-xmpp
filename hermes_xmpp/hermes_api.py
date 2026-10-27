"""Thin client for the Hermes API server (Runs API + Chat Completions).

Kept to the standard library on purpose: the daemon's only third-party
dependency is the XMPP library.

The bridge drives one run per contact through ``POST /v1/runs``: a run has an
id, can be polled, and — crucially — can be stopped with
``POST /v1/runs/{id}/stop``. That is what lets a newer message interrupt the
answer the model is still writing. The Chat Completions route is kept only for
turns that carry inline images, which the Runs API does not accept.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Endpoint

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
POLL_INTERVAL_S = 0.75
# Mid-turn assistant commentary ("hold on, let me look into that") rides this event.
INTERIM_EVENT = "message.interim"


class BridgeError(Exception):
    """A failure that has a safe, short message for the contact."""

    def __init__(self, user_message: str, detail: str = "", status: int = 0,
                 code: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.status = status
        self.code = code


class SessionRetired(BridgeError):
    """The session id names a session that was deleted or belongs elsewhere.

    Hermes keeps a tombstone (``gateway.retired_session.v1.<id>``) when a
    session is deleted, and refuses that id forever with ``409 not_found``. A
    derived id therefore has to be replaceable at runtime.
    """


@dataclass(frozen=True)
class Turn:
    """One request to the agent."""

    system: str
    user: str
    session_key: str
    images: tuple[str, ...] = ()   # data: URLs; chat-completions route only


@dataclass(frozen=True)
class Answer:
    """The reply plus the run/status that produced it."""

    text: str
    status: str = "completed"
    run_id: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.status in ("cancelled", "interrupted")


def _request(endpoint: Endpoint, api_key: str, path: str, *, method: str = "GET",
             body: dict | None = None, timeout: float | None = None,
             session_key: str = "") -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Hermes-Source": "xmpp",
    }
    if session_key:
        headers["X-Hermes-Session-Key"] = session_key
    request = urllib.request.Request(
        endpoint.runs_base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout or endpoint.timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        error_code = _error_code(detail)
        cls = BridgeError
        if exc.code == 409 and error_code in ("not_found", "permission_denied"):
            # The session id cannot be used: deleted (tombstoned) or owned by
            # another surface. The caller rotates to a fresh id and retries.
            cls = SessionRetired
        raise cls(_friendly_http(exc.code), f"HTTP {exc.code}: {detail}",
                  status=exc.code, code=error_code) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise BridgeError("My backend is not responding right now.", str(exc)) from exc


def _error_code(detail: str) -> str:
    """``code`` from an OpenAI-style error envelope, else ''."""
    try:
        payload = json.loads(detail)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return str(error.get("code") or "")
        return str(payload.get("code") or "") if isinstance(payload, dict) else ""
    except (ValueError, TypeError):
        return ""


def _friendly_http(code: int) -> str:
    if code in (401, 403):
        return ("I can't reach my brain right now (authentication failed). "
                "The operator needs to check the bridge API key.")
    if code == 409:
        return "That conversation is out of sync on my side. Try again in a moment."
    if code == 429:
        return "My backend is busy right now. Try again in a moment."
    if code == 503:
        return "My backend is restarting right now. Try again in a moment."
    return f"My backend returned an error (HTTP {code})."


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001 - diagnostics only
        return str(exc)


def _payload(turn: Turn, endpoint: Endpoint) -> dict:
    body: dict = {"input": turn.user, "model": endpoint.model}
    if endpoint.provider:
        # Per-request routing: provider > model over the profile defaults.
        body["provider"] = endpoint.provider
    if turn.system:
        # ``instructions`` is the ephemeral system prompt: never persisted.
        body["instructions"] = turn.system
    return body


def start(endpoint: Endpoint, api_key: str, turn: Turn, session_id: str) -> str:
    """Admit one run; returns its id."""
    body = _payload(turn, endpoint)
    if session_id:
        body["session_id"] = session_id
    _, raw = _request(endpoint, api_key, "/v1/runs", method="POST", body=body,
                      session_key=turn.session_key)
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise BridgeError("My backend sent something I could not parse.",
                          raw[:200].decode("utf-8", "replace")) from exc
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise BridgeError("My backend did not accept that message.",
                          raw[:400].decode("utf-8", "replace"))
    return run_id


def status(endpoint: Endpoint, api_key: str, run_id: str, session_key: str = "") -> dict:
    _, raw = _request(endpoint, api_key, f"/v1/runs/{run_id}", session_key=session_key)
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise BridgeError("My backend sent something I could not parse.",
                          raw[:200].decode("utf-8", "replace")) from exc


def stop(endpoint: Endpoint, api_key: str, run_id: str) -> None:
    """Ask the server to interrupt a run. Best effort: an ended run is fine."""
    try:
        _request(endpoint, api_key, f"/v1/runs/{run_id}/stop", method="POST",
                 body={}, timeout=15)
    except BridgeError:
        pass


def wait(endpoint: Endpoint, api_key: str, run_id: str, *, session_key: str = "",
         timeout: float | None = None) -> Answer:
    """Poll one run until it is terminal; return its final text."""
    deadline = time.monotonic() + (timeout or endpoint.timeout_s)
    while True:
        state = status(endpoint, api_key, run_id, session_key)
        name = str(state.get("status") or "")
        if name in TERMINAL:
            text = str(state.get("output") or "").strip()
            if name == "completed":
                return Answer(text=text, status=name, run_id=run_id)
            if name in ("cancelled", "interrupted"):
                return Answer(text="", status=name, run_id=run_id)
            # failed: the server's text explains why (billing, provider error).
            raise BridgeError(text or "My backend failed on that turn.",
                              f"run {run_id} status={name}")
        if time.monotonic() >= deadline:
            stop(endpoint, api_key, run_id)
            raise BridgeError("That took too long, so I stopped working on it.",
                              f"run {run_id} timed out")
        time.sleep(POLL_INTERVAL_S)


def ask(endpoint: Endpoint, api_key: str, turn: Turn, session_id: str = "") -> Answer:
    """One self-contained run: start, wait, return."""
    run_id = start(endpoint, api_key, turn, session_id)
    return wait(endpoint, api_key, run_id, session_key=turn.session_key)


def image_url(value: str, max_bytes: int = 6 * 1024 * 1024) -> str:
    """A spool file path as the ``data:`` URL the completions route accepts.

    The route rejects plain paths ("Image inputs must use http(s) URLs or
    data:image/... URLs"), so whatever the XMPP client sent arrives here as a file
    and leaves as base64.
    """
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BridgeError("I could not open the image you sent.", str(exc)) from exc
    if len(raw) > max_bytes:
        raise BridgeError("That image is too large for me to look at.",
                          f"{len(raw)} bytes")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise BridgeError("That file is not an image I can open.", mime)
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def build_payload(turn: Turn, model: str, provider: str = "") -> dict:
    """Chat Completions payload (image turns only)."""
    messages: list[dict] = []
    if turn.system:
        messages.append({"role": "system", "content": turn.system})
    if turn.images:
        content: list[dict] = [{"type": "text", "text": turn.user}]
        content += [{"type": "image_url", "image_url": {"url": image_url(url)}}
                    for url in turn.images]
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": turn.user})
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if provider:
        payload["provider"] = provider
    return payload


def ask_with_images(endpoint: Endpoint, api_key: str, turn: Turn,
                    session_id: str = "") -> Answer:
    """Chat Completions route: only used when the turn carries inline images."""
    body = json.dumps(build_payload(turn, endpoint.model, endpoint.provider)).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Hermes-Session-Key": turn.session_key,
        "X-Hermes-Source": "xmpp",
    }
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id
    request = urllib.request.Request(endpoint.chat_url, data=body, method="POST",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=endpoint.timeout_s) as response:
            raw = response.read()
            returned = response.headers.get("X-Hermes-Session-Id") or session_id
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        raise BridgeError(_friendly_http(exc.code), f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise BridgeError("My backend is not responding right now.", str(exc)) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeError("My backend sent something I could not parse.",
                          raw[:200].decode("utf-8", "replace")) from exc
    text = _extract_content(payload).strip()
    if not text:
        code = payload.get("hermes", {}).get("error_code") if isinstance(payload.get("hermes"), dict) else None
        raise BridgeError("I had no answer for that one.", f"empty content, code={code}")
    return Answer(text=text, status="completed", run_id=str(returned or "") or None)


def iter_sse_events(lines) -> Iterator[dict]:
    """Yield decoded event dicts from an SSE byte/str line stream.

    Frames are ``data:`` lines separated by a blank line; comment frames (used as
    keepalives and replay markers) start with ``:`` and are skipped.
    """
    buffer: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if not line:
            if buffer:
                payload, buffer = "\n".join(buffer), []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        try:
            yield json.loads("\n".join(buffer))
        except json.JSONDecodeError:
            return


def stream_run_events(endpoint: Endpoint, api_key: str, run_id: str,
                      on_event, stop=None) -> None:
    """Block on ``GET /v1/runs/{id}/events`` and hand each event to ``on_event``.

    Runs in a worker thread; returns when the run reaches a terminal event, the
    caller sets ``stop``, or the connection ends. Every failure is swallowed: the
    final answer still arrives through the normal polling path, so live
    commentary is a bonus, never a dependency.
    """
    url = f"{endpoint.runs_base}/v1/runs/{run_id}/events"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    })
    try:
        with urllib.request.urlopen(request, timeout=endpoint.timeout_s) as response:
            for event in iter_sse_events(response):
                if not isinstance(event, dict):
                    continue
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - a bad consumer must not kill the stream
                    pass
                name = str(event.get("event") or "")
                if name in {f"run.{state}" for state in TERMINAL}:
                    return
                if stop is not None and stop.is_set():
                    return
    except Exception:  # noqa: BLE001 - polling still delivers the answer
        return


def _extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""
