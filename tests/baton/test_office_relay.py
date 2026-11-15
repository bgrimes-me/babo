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


def test_the_surface_is_closed_a_field_nobody_knows_is_not_forwarded() -> None:
    r, _ = _relay((200, '{"in_flight": true, "queue_depth": 7}'))
    assert r.status() == {"seen": True, "in_flight": True}


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
    assert "timeout after 30.0s" in said


def test_message_refuses_attachments_by_name_without_calling_the_door() -> None:
    r, door = _relay()
    body = r.message(source="@evans:matrix.home", channel="matrix", content="hi",
                     attachments=("cat.png",))
    _one_failure_shape(body, "message")
    assert "attachments" in body["unknown"] and door.calls == []


# -- the relay's own bind and its own body are not this module's job --------
def test_handlers_choose_no_status_code_at_all() -> None:
    r, _ = _relay((200, '{"in_flight": false, "accepted": true, "persisted": true}'))
    bodies = [r.health(), r.status(), r.quiesce("r"), r.flush("r"),
              r.message(source="s", channel="c", content="x")]
    assert all(isinstance(body, dict) for body in bodies)
    assert all("status" not in body and "code" not in body for body in bodies)
