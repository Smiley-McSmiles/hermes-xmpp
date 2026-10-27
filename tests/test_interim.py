import io
import json

from hermes_xmpp import hermes_api
from hermes_xmpp.config import Endpoint


def _sse(*events, raw_comments=True):
    lines = []
    if raw_comments:
        lines.append(b": open\n")
        lines.append(b"\n")
    for event in events:
        lines.append(f"data: {json.dumps(event)}\n".encode())
        lines.append(b"\n")
    return lines


def test_iter_sse_events_skips_comments_and_decodes_frames():
    lines = _sse({"event": "tool.started", "name": "web_search"},
                 {"event": "message.interim", "text": "Hold on, let me look into that."})
    events = list(hermes_api.iter_sse_events(lines))
    assert [e["event"] for e in events] == ["tool.started", "message.interim"]
    assert events[1]["text"] == "Hold on, let me look into that."


def test_iter_sse_events_tolerates_garbage_and_multiline_data():
    lines = [b"data: not-json\n", b"\n", b'data: {"event": "x",\n', b'data:  "seq": 3}\n', b"\n"]
    events = list(hermes_api.iter_sse_events(lines))
    assert events == [{"event": "x", "seq": 3}]


def test_stream_run_events_calls_back_and_stops_at_a_terminal_event(monkeypatch):
    body = _sse({"event": "message.interim", "text": "looking"},
                {"event": "run.completed", "status": "completed"},
                {"event": "message.interim", "text": "never reached"})

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    monkeypatch.setattr(hermes_api.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(b"".join(body)))
    endpoint = Endpoint(url="http://127.0.0.1:8642/v1/chat/completions", key_file="unused")
    captured = []
    hermes_api.stream_run_events(endpoint, "key", "run-1", captured.append)
    assert [e["event"] for e in captured] == ["message.interim", "run.completed"]


def test_stream_run_events_swallows_transport_errors(monkeypatch):
    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(hermes_api.urllib.request, "urlopen", boom)
    endpoint = Endpoint(url="http://127.0.0.1:8642/v1/chat/completions", key_file="unused")
    hermes_api.stream_run_events(endpoint, "key", "run-1", lambda event: None)
