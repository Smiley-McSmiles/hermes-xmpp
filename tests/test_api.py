import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_xmpp.config import Endpoint
from hermes_xmpp.hermes_api import (BridgeError, SessionRetired, Turn, ask,
                                    ask_with_images, build_payload, start, stop, wait)


class _Handler(BaseHTTPRequestHandler):
    """Minimal Hermes Runs API: create, poll, stop (+ chat completions)."""

    run_id = "run_test1"
    start_status = 202
    error_code = "authentication_failed"
    polls_before_done = 1
    final_status = "completed"
    final_output = "hi there"
    seen_start: dict = {}
    seen_poll: dict = {}
    stopped: list = []
    poll_count = 0

    @classmethod
    def reset(cls):
        cls.start_status = 202
        cls.polls_before_done = 1
        cls.final_status = "completed"
        cls.final_output = "hi there"
        cls.seen_start = {}
        cls.seen_poll = {}
        cls.stopped = []
        cls.poll_count = 0

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/stop"):
            run_id = self.path.rstrip("/").split("/")[-2]
            _Handler.stopped.append(run_id)
            return self._send(200, {"run_id": run_id, "status": "stopping"})
        if self.path.endswith("/v1/runs"):
            _Handler.seen_start = {"headers": dict(self.headers), "body": body}
            if _Handler.start_status != 202:
                return self._send(_Handler.start_status,
                                  {"error": {"message": "bad key sekret",
                                             "code": _Handler.error_code}})
            return self._send(202, {"run_id": _Handler.run_id, "status": "started"})
        if self.path.endswith("/v1/chat/completions"):
            _Handler.seen_start = {"headers": dict(self.headers), "body": body}
            return self._send(200, {"choices": [{"message": {"role": "assistant",
                                                             "content": "image answer"}}]})
        return self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        _Handler.poll_count += 1
        _Handler.seen_poll = {"headers": dict(self.headers)}
        run_id = self.path.rstrip("/").split("/")[-1]
        if run_id != _Handler.run_id:
            return self._send(404, {"error": "no such run"})
        done = _Handler.poll_count >= _Handler.polls_before_done
        status = _Handler.final_status if done else "running"
        return self._send(200, {"object": "hermes.run", "run_id": run_id, "status": status,
                                "output": _Handler.final_output if done else ""})

    def log_message(self, *args):  # silence
        pass


@pytest.fixture()
def server():
    _Handler.reset()
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Endpoint(url=f"http://127.0.0.1:{httpd.server_port}/p/xmpp-owner/v1/chat/completions",
                   key_file="unused")
    httpd.shutdown()


def _headers(seen):
    return {k.lower(): v for k, v in seen["headers"].items()}


def test_runs_base_is_derived_from_the_completions_url(server):
    assert server.runs_base == f"{server.url.rsplit('/v1/', 1)[0]}"
    assert server.chat_url == server.url


def test_start_posts_a_run_with_session_and_ephemeral_system(server):
    run_id = start(server, "sekret",
                   Turn(system="sys", user="hello", session_key="xmpp:abc"), "xmpp-123")
    assert run_id == "run_test1"
    body = _Handler.seen_start["body"]
    assert body == {
        "input": "hello",
        "model": "hermes-agent",
        "instructions": "sys",
        "session_id": "xmpp-123",
    }
    headers = _headers(_Handler.seen_start)
    assert _Handler.seen_start["headers"] is not None
    assert headers["authorization"] == "Bearer sekret"
    assert headers["x-hermes-session-key"] == "xmpp:abc"


def test_start_sends_provider_for_per_request_routing(server):
    endpoint = Endpoint(url=server.url, key_file="unused",
                        model="deepseek/deepseek-v4.1-flash-1017", provider="openrouter")
    start(endpoint, "k", Turn(system="", user="hi", session_key="k"), "xmpp-1")
    assert _Handler.seen_start["body"]["provider"] == "openrouter"


def test_wait_polls_until_completed_and_returns_output(server):
    answer = wait(server, "k", "run_test1")
    assert answer.text == "hi there"
    assert answer.status == "completed"
    assert answer.cancelled is False
    assert _Handler.poll_count >= 1
    assert _headers(_Handler.seen_poll)["authorization"] == "Bearer k"


def test_wait_returns_empty_text_for_a_cancelled_run(server):
    _Handler.final_status = "cancelled"
    _Handler.final_output = "Operation interrupted: waiting for model response"
    answer = wait(server, "k", "run_test1")
    assert answer.cancelled is True
    assert answer.text == ""


def test_wait_raises_with_the_server_explanation_on_failure(server):
    _Handler.final_status = "failed"
    _Handler.final_output = "Billing or credits exhausted"
    with pytest.raises(BridgeError, match="Billing"):
        wait(server, "k", "run_test1")


def test_stop_posts_to_the_run_stop_endpoint(server):
    stop(server, "k", "run_test1")
    assert _Handler.stopped == ["run_test1"]


def test_auth_failure_is_reported_without_leaking_the_key(server):
    _Handler.start_status = 401
    with pytest.raises(BridgeError) as excinfo:
        start(server, "sekret", Turn(system="", user="u", session_key="k"), "xmpp-1")
    assert "authentication" in excinfo.value.user_message.lower()
    assert "sekret" not in excinfo.value.user_message


def test_ask_start_then_wait_roundtrip(server):
    answer = ask(server, "k", Turn(system="", user="u", session_key="k"), "xmpp-1")
    assert answer.text == "hi there"
    assert answer.run_id == "run_test1"


def test_a_tombstoned_session_raises_session_retired(server):
    _Handler.start_status = 409
    _Handler.error_code = "not_found"
    with pytest.raises(SessionRetired) as excinfo:
        start(server, "k", Turn(system="", user="u", session_key="k"), "xmpp-retired")
    assert excinfo.value.status == 409
    assert excinfo.value.code == "not_found"


def test_other_409s_stay_plain_bridge_errors(server):
    _Handler.start_status = 409
    _Handler.error_code = "idempotency_key_conflict"
    with pytest.raises(BridgeError) as excinfo:
        start(server, "k", Turn(system="", user="u", session_key="k"), "xmpp-x")
    assert not isinstance(excinfo.value, SessionRetired)


# -- chat completions path (inline images) -----------------------------------

def test_payload_includes_system_then_user():
    payload = build_payload(Turn(system="sys", user="hello", session_key="k"), "hermes-agent")
    assert payload["model"] == "hermes-agent"
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


def test_payload_omits_empty_system():
    payload = build_payload(Turn(system="", user="hi", session_key="k"), "m")
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_image_turn_uses_chat_completions_with_the_session_id(server):
    turn = Turn(system="s", user="what is this?", session_key="xmpp:abc",
                images=("data:image/png;base64,AAAA",))
    answer = ask_with_images(server, "sekret", turn, "xmpp-123")
    assert answer.text == "image answer"
    body = _Handler.seen_start["body"]
    assert body["messages"][-1]["content"][1]["type"] == "image_url"
    assert _headers(_Handler.seen_start)["x-hermes-session-id"] == "xmpp-123"
