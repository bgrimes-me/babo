"""The office relay: five calls, one shape, and the rules that keep it honest.

The contract is ``docs/OFFICE-RELAY.md``. Each row here is one sentence from it,
made runnable with a fake door instead of a pod — which is the whole point of the
transport being injected: these run on a runner, in CI, with no container.

The two rows acceptance hangs on are named for what they forbid:

- ``test_an_unreachable_door_is_unknown_with_no_success_fields`` — a blind relay
  may not answer ``seen: false``;
- ``test_quiesce_with_a_turn_in_flight_reports_the_turn`` — a relay that answers
  success while a turn runs has decided instead of reported, and every consumer
  then believes the pod is safe to stop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from babo.office import relay


class FakeDoor:
    """The agent's local door, scripted. Answers, refuses, times out, talks nonsense."""

    def __init__(self, *answers: tuple[int, str] | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, body: dict[str, Any] | None,
                 timeout_s: float) -> tuple[int, str]:
        self.calls.append({"method": method, "url": url, "body": body, "timeout_s": timeout_s})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _relay(*answers: tuple[int, str] | BaseException, **kwargs: Any) -> tuple[relay.Relay, FakeDoor]:
    door = FakeDoor(*answers)
    return relay.Relay("http://127.0.0.1:8844", transport=door, **kwargs), door


def _one_failure_shape(body: dict[str, Any], call: str) -> None:
    """The house shape, asserted as a shape: one key, a string, this call's name."""
    assert set(body) == {"unknown"}, f"not one key: {sorted(body)}"
    said = body["unknown"]
    assert isinstance(said, str) and said.startswith(f"{call}: "), said
    assert len(said) > len(call) + 2


# -- health: the one thing the relay can know without asking ------------------
def test_health_answers_without_asking_the_door_anything() -> None:
    r, door = _relay(**{"clock": lambda: 100.0})
    body = r.health()
    assert door.calls == []
    assert body["ok"] is True and body["relay"]["version"] == relay.RELAY_VERSION


def test_health_uptime_comes_from_the_injected_clock() -> None:
    now = [100.0]
    r = relay.Relay("http://127.0.0.1:8844", transport=FakeDoor(), clock=lambda: now[0])
    now[0] = 142.7
    assert r.health()["relay"]["uptime_s"] == 42


def test_health_never_carries_unknown() -> None:
    r, _ = _relay()
    assert "unknown" not in r.health()


# -- the one failure shape, at every call site -------------------------------
def test_an_unreachable_door_is_unknown_with_no_success_fields() -> None:
    r, _ = _relay(OSError("connection refused"))
    body = r.status()
    _one_failure_shape(body, "status")
    # The claim acceptance forbids: a blind relay may not say "no turn is running".
    assert "in_flight" not in body and "seen" not in body
    assert "connection refused" in body["unknown"]


def test_every_serviceless_call_fails_in_the_same_shape() -> None:
    for call, invoke in (
        ("status", lambda r: r.status()),
        ("quiesce", lambda r: r.quiesce("leaked sessions")),
        ("flush", lambda r: r.flush("quiesced for restart")),
        ("message", lambda r: r.message(source="@evans:matrix.home", channel="matrix", content="hi")),
    ):
        r, _ = _relay(OSError("connection refused"))
        _one_failure_shape(invoke(r), call)


def test_a_status_timeout_is_named_as_a_timeout() -> None:
    r, _ = _relay(TimeoutError("timed out"), status_timeout_s=2.0)
    said = r.status()["unknown"]
    assert said.startswith("status: ") and "timeout after 2.0s" in said


def test_a_quiesce_timeout_is_named_as_a_timeout() -> None:
    r, _ = _relay(TimeoutError("timed out"), quiesce_timeout_s=5.0)
    said = r.quiesce("sessions leaked")["unknown"]
    assert said.startswith("quiesce: ") and "timeout after 5.0s" in said


def test_a_nonsense_body_is_unknown_not_an_exception() -> None:
    r, _ = _relay((200, "not json at all"))
    _one_failure_shape(r.status(), "status")


def test_a_json_body_that_is_not_an_object_is_unknown() -> None:
    r, _ = _relay((200, "[1, 2, 3]"))
    assert "not a JSON object" in r.status()["unknown"]


def test_an_empty_body_is_unknown_not_a_zero() -> None:
    r, _ = _relay((200, "   "))
    body = r.status()
    _one_failure_shape(body, "status")
    assert "not a JSON object" in body["unknown"]


# -- status: silence is not idle ---------------------------------------------
def test_status_passes_the_turn_position_through() -> None:
    r, door = _relay((200, '{"in_flight": true, "turn_id": "t-9f21", '
                           '"wall_started_at": "2026-11-14T22:10:00Z"}'))
    body = r.status()
    assert body == {"seen": True, "in_flight": True, "turn_id": "t-9f21",
                    "wall_started_at": "2026-11-14T22:10:00Z"}
    assert door.calls[0]["method"] == "GET"
    assert door.calls[0]["url"].endswith("/v1/office/status")


def test_status_is_idle_only_when_the_agent_says_so() -> None:
    r, _ = _relay((200, '{"in_flight": false, "turn_id": null, "wall_started_at": null}'))
    assert r.status() == {"seen": True, "in_flight": False, "turn_id": None, "wall_started_at": None}


def test_status_without_a_turn_position_is_unknown() -> None:
    r, _ = _relay((200, '{"seen": true}'))
    body = r.status()
    _one_failure_shape(body, "status")
    assert "in flight" in body["unknown"]


def test_a_position_that_cannot_name_the_turn_is_unknown() -> None:
    """Half a claim is not a position: a running turn the relay cannot name is no use."""
    r, _ = _relay((200, '{"in_flight": true}'))
    body = r.status()
    _one_failure_shape(body, "status")
    assert "did not name it" in body["unknown"]
    assert "in_flight" not in body and "turn_id" not in body


def test_quiesce_does_not_forward_half_a_position_either() -> None:
    r, _ = _relay((200, '{"accepted": true, "in_flight": true, "turn_id": "  "}'))
    body = r.quiesce("sessions leaked")
    _one_failure_shape(body, "quiesce")
    assert "accepted" not in body


def test_the_surface_is_closed_a_field_nobody_knows_is_not_forwarded() -> None:
    r, _ = _relay((200, '{"in_flight": true, "turn_id": "t-1", "queue_depth": 7}'))
    assert r.status() == {"seen": True, "in_flight": True, "turn_id": "t-1"}


# -- quiesce: a turn in flight is reported, not quiesced ---------------------
def test_quiesce_with_a_turn_in_flight_reports_the_turn() -> None:
    r, door = _relay((200, '{"accepted": true, "in_flight": true, "turn_id": "t-9f21", '
                           '"wall_started_at": "2026-11-14T22:10:00Z"}'))
    body = r.quiesce("sessions leaked")
    assert body["accepted"] is True
    assert body["in_flight"] is True and body["turn_id"] == "t-9f21"
    # The word the report must not contain: there is no such field, and a
    # consumer that finds one will believe the pod is safe to stop.
    assert "quiesced" not in body
    assert door.calls[0]["body"] == {"reason": "sessions leaked"}


def test_quiesce_with_no_turn_says_so_without_inventing_idle() -> None:
    r, _ = _relay((200, '{"accepted": true, "in_flight": false}'))
    assert r.quiesce("sessions leaked") == {"accepted": True, "in_flight": False}


def test_a_refusal_by_the_agent_is_its_own_sentence() -> None:
    sentence = "a turn is mid-tool-call; retry after the boundary"
    r, _ = _relay((409, '{"detail": "a turn is mid-tool-call; retry after the boundary"}'))
    body = r.quiesce("sessions leaked")
    assert body == {"detail": sentence}
    assert "unknown" not in body


def test_a_refusal_whose_detail_is_not_a_sentence_is_not_an_answer() -> None:
    r, _ = _relay((409, '{"detail": 5}'))
    _one_failure_shape(r.quiesce("sessions leaked"), "quiesce")


def test_an_error_answer_that_is_not_json_carries_no_sentence() -> None:
    r, _ = _relay((502, "<html>bad gateway</html>"))
    body = r.quiesce("sessions leaked")
    _one_failure_shape(body, "quiesce")
    assert "502" in body["unknown"]


def test_a_quiesce_the_agent_declined_is_unknown_not_accepted() -> None:
    r, _ = _relay((200, '{"accepted": false, "in_flight": false}'))
    body = r.quiesce("sessions leaked")
    _one_failure_shape(body, "quiesce")
    assert "without accepting" in body["unknown"] and "accepted" not in body


def test_a_missing_route_is_not_a_refusal() -> None:
    """Until item 2 lands, 404 is the truth about quiesce, and it is not a sentence."""
    r, _ = _relay((404, '{"detail": "Not Found"}'))
    body = r.quiesce("sessions leaked")
    _one_failure_shape(body, "quiesce")
    assert "/v1/office/quiesce" in body["unknown"] and "404" in body["unknown"]


# -- flush: no persist it cannot substantiate -------------------------------
def test_flush_claims_a_persist_only_when_the_agent_confirmed() -> None:
    r, door = _relay((200, '{"persisted": true, "detail": "the agent confirmed its store was written"}'))
    body = r.flush("quiesced for restart")
    assert body == {"persisted": True, "detail": "the agent confirmed its store was written"}
    assert door.calls[0]["url"].endswith("/v1/office/flush")
    assert door.calls[0]["body"] == {"reason": "quiesced for restart"}


def test_flush_does_not_turn_a_nonconfirmation_into_a_persist() -> None:
    r, _ = _relay((200, '{"persisted": false}'))
    body = r.flush("quiesced for restart")
    _one_failure_shape(body, "flush")
    assert "persisted" not in body


# -- message: delivered is the only thing a caller is told ------------------
def test_message_is_delivered_on_a_2xx_and_says_nothing_else() -> None:
    r, _ = _relay((200, "{}"))
    assert r.message(source="@evans:matrix.home", channel="matrix", content="hello") == {"delivered": True}


def test_message_posts_one_user_message_and_names_the_sender() -> None:
    r, door = _relay((200, "{}"))
    r.message(source="@evans:matrix.home", channel="matrix", content="hello")
    call = door.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/chat/completions")
    assert call["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert call["body"]["session_id"] == "matrix:@evans:matrix.home"
    # The door compares "model" against its configured model and 400s on a
    # mismatch; the relay has no business choosing one.
    assert "model" not in call["body"]


def test_message_four_xx_is_unknown_even_when_the_door_explains() -> None:
    r, _ = _relay((400, '{"detail": "Only a single user message is supported"}'))
    body = r.message(source="@evans:matrix.home", channel="matrix", content="hi")
    _one_failure_shape(body, "message")
    assert "delivered" not in body
    # The words survive inside the reason; they are not an outcome.
    assert "Only a single user message is supported" in body["unknown"]
    assert "400" in body["unknown"]


def test_message_five_xx_is_unknown() -> None:
    r, _ = _relay((503, '{"detail": "Model is not configured."}'))
    body = r.message(source="@evans:matrix.home", channel="matrix", content="hi")
    _one_failure_shape(body, "message")
    assert "503" in body["unknown"] and "delivered" not in body


def test_message_refusal_is_unknown_not_delivered_false() -> None:
    r, _ = _relay(OSError("Connection refused"))
    body = r.message(source="@evans:matrix.home", channel="matrix", content="hi")
    _one_failure_shape(body, "message")
    assert "delivered" not in body


def test_message_timeout_is_unknown() -> None:
    r, _ = _relay(TimeoutError("timed out"))
    said = r.message(source="@evans:matrix.home", channel="matrix", content="hi")["unknown"]
    assert "within 30.0s" in said
    # A caller reading `unknown` must not conclude it never arrived: the door we
    # use takes a whole turn, so a deadline leaves the turn running or not taken.
    assert "whether it took the turn is unknown" in said


def test_a_2xx_from_this_door_means_a_turn_was_taken_not_that_it_queued() -> None:
    """The semantic of the door behind `message`, asserted where it is decided.

    /v1/chat/completions carries a turn (one user message, awaited to completion),
    so `delivered` is as strong as that door can be. /v1/office/message is the
    route where a 2xx will mean accepted-for-delivery.
    """
    r, door = _relay((200, '{"choices": [{"message": {"content": "done"}}]}'))
    assert r.message(source="@evans:matrix.home", channel="matrix", content="hello") == {"delivered": True}
    sent = door.calls[0]["body"]
    assert list(sent) == ["messages", "session_id"], sent
    assert sent["messages"][0]["role"] == "user"


def test_message_refuses_attachments_by_name_without_calling_the_door() -> None:
    r, door = _relay()
    body = r.message(source="@evans:matrix.home", channel="matrix", content="hi",
                     attachments=("cat.png",))
    _one_failure_shape(body, "message")
    assert "attachments" in body["unknown"] and door.calls == []


def _door_server(status: int, payload: bytes, seen: list[dict[str, Any]]) -> tuple[HTTPServer, threading.Thread]:
    """One real socket that answers once, so the default transport is exercised."""

    class Door(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            seen.append({"path": self.path, "body": json.loads(raw)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), Door)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    return server, worker


def test_the_default_transport_speaks_to_a_real_socket() -> None:
    """The fake door proves the relay. This proves the transport underneath it."""
    seen: list[dict[str, Any]] = []
    server, worker = _door_server(200, b'{"accepted": true, "in_flight": false}', seen)
    try:
        r = relay.Relay(f"http://127.0.0.1:{server.server_port}")
        assert r.quiesce("sessions leaked") == {"accepted": True, "in_flight": False}
        assert seen == [{"path": relay.AGENT_QUIESCE, "body": {"reason": "sessions leaked"}}]
    finally:
        worker.join(timeout=5)
        server.server_close()


def test_the_default_transport_reads_a_real_refusal() -> None:
    """A 4xx on the wire is the agent's sentence, not a connection failure."""
    seen: list[dict[str, Any]] = []
    server, worker = _door_server(409, b'{"detail": "a turn is mid-tool-call"}', seen)
    try:
        r = relay.Relay(f"http://127.0.0.1:{server.server_port}")
        assert r.quiesce("sessions leaked") == {"detail": "a turn is mid-tool-call"}
    finally:
        worker.join(timeout=5)
        server.server_close()


# -- the relay's own bind and its own body are not this module's job --------
def test_handlers_choose_no_status_code_at_all() -> None:
    r, _ = _relay((200, '{"in_flight": false, "accepted": true, "persisted": true}'))
    bodies = [r.health(), r.status(), r.quiesce("r"), r.flush("r"),
              r.message(source="s", channel="c", content="x")]
    assert all(isinstance(body, dict) for body in bodies)
    assert all("status" not in body and "code" not in body for body in bodies)
