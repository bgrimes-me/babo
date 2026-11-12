"""The UI→Manager hop: the calls in one direction, and the rules that guard them.

Client side: a failure is a value, the Manager's words are the verdict, and no
retry happens silently. Service side: only the UI pod gets in to the agent path,
actor and reason are required, a replay key is the caller's, and the terminal set
stays closed. One verb has a different door: `restart-ui` is the host operator's,
admitted at the host-WG door, and 202 on it is best-effort — no client ever sees
it reach `succeeded` (H-6).
"""

from __future__ import annotations

import json

from babo.office import hop, service
from babo.office.actors import Actor
from babo.office.model import AgentObserved
from babo.office.service import ManagerService
from baton.office_fakes import FakeCore

UI = "10.0.0.11"
OP = "10.77.0.9"


def _core(**kwargs: object) -> FakeCore:
    return FakeCore(agents_by_handle={"crew-1": AgentObserved(handle="crew-1", state="frozen")}, **kwargs)  # type: ignore[arg-type]


# -- client -----------------------------------------------------------------
def test_a_transport_failure_is_a_value_never_an_exception() -> None:
    def broken(method: str, url: str, body: object, timeout_s: float) -> tuple[int, str]:
        raise OSError("connection refused")

    client = hop.HopClient("http://manager.invalid:8844", transport=broken)
    result = client.health()
    assert result.unavailable and "connection refused" in result.said
    assert result.payload == {}


def test_the_client_passes_the_managers_words_through_untouched() -> None:
    seen: list[dict[str, object]] = []

    def transport(method: str, url: str, body: dict | None, timeout_s: float) -> tuple[int, str]:
        seen.append({"method": method, "url": url, "body": body})
        return 202, json.dumps({"action_id": "act-1"})

    client = hop.HopClient("http://manager.invalid:8844", transport=transport)
    result = client.act("crew-1", "wake", "owner asked", actor=Actor("owner", "webui-session"), action_id="k1")
    assert result.ok and result.payload["action_id"] == "act-1"
    assert seen[0]["body"] == {"handle": "crew-1", "action": "wake", "reason": "owner asked",
                               "actor": "owner", "actor_source": "webui-session", "action_id": "k1"}


def test_the_client_refuses_an_unknown_verb_without_a_round_trip() -> None:
    calls: list[object] = []

    def transport(*args: object) -> tuple[int, str]:  # pragma: no cover - must not be called
        calls.append(args)
        return 202, "{}"

    result = hop.HopClient("http://x", transport=transport).act("h", "explode", "r", actor=Actor("o", "terminal"))
    assert result.unavailable and "unknown action" in result.said and not calls


def test_the_client_refuses_the_pod_verb_on_the_agent_path_and_from_a_session() -> None:
    """The UI derives the operator; it does not invent one, and it does not carry
    a pod verb on the agent path (ruled 2026-11-11).

    Three facts, in the order the door checks them: the generic `act` never
    carries `restart-ui`; a session-sourced actor is refused here rather than
    becoming a 403; and a derived host-WG operator is sent to the operator
    endpoint with that actor, because that is the only spelling the Manager admits.
    """
    calls: list[dict[str, object]] = []

    def transport(method: str, url: str, body: dict | None, timeout_s: float) -> tuple[int, str]:
        calls.append({"method": method, "url": url, "body": body})
        return 202, json.dumps({"action_id": "act-1", "status": "accepted", "best_effort": True})

    client = hop.HopClient("http://x", transport=transport)

    via_act = client.act("babo-ui", "restart-ui", "r", actor=Actor("evans", "host-wg"))
    assert via_act.unavailable and "agent path" in via_act.said

    from_a_session = client.restart_ui("r", actor=Actor("owner", "webui-session"))
    assert from_a_session.unavailable and "provenance is not the host operator" in from_a_session.said
    assert not calls, "neither refusal left the pod"

    derived = client.restart_ui("invalidate sessions", actor=hop.operator_from_session("evans", "host-wg"))
    assert derived.ok and derived.status == 202
    assert calls == [{"method": "POST", "url": "http://x/api/v1/office/ui/restart",
                      "body": {"reason": "invalidate sessions", "actor": "evans",
                               "actor_source": "host-wg"}}]


def test_an_operator_cannot_be_derived_from_anything_but_a_host_wg_session() -> None:
    """One place derives the operator, so the surface cannot grow a second idea.

    Both halves travel together (F-7): a name without provenance is a string that
    satisfied a check, not an identity; a provenance without a name is anonymous.
    """
    import pytest

    for source in ("webui-session", "channel", "terminal", ""):
        with pytest.raises(ValueError):
            hop.operator_from_session("evans", source)
    with pytest.raises(ValueError):
        hop.operator_from_session("   ", "host-wg")
    assert hop.operator_from_session("evans", "host-wg") == Actor("evans", "host-wg")


# -- service ----------------------------------------------------------------
def test_only_the_ui_pod_gets_in() -> None:
    svc = ManagerService(core=_core(), ui_peers=(UI,), operator_peers=(OP,))
    assert svc.agents(UI)[0] == 200
    status, payload = svc.agents("10.0.0.99")
    assert status == 403 and "not the UI pod" in payload["detail"]
    # an exact match is the rule: a neighbour address is not "close enough"
    assert svc.act("10.0.0.110", {"handle": "crew-1", "action": "wake", "reason": "r",
                                 "actor": "owner", "actor_source": "webui-session"})[0] == 403


def test_an_anonymous_action_is_refused_before_any_effect() -> None:
    core = _core()
    svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))
    status, payload = svc.act(UI, {"handle": "crew-1", "action": "wake", "reason": "r"})
    assert status == 400 and "must name who is asking" in payload["detail"]
    assert not core.audit, "no effect without an actor"


def test_actor_and_reason_travel_to_the_core_with_their_source() -> None:
    core = _core()
    svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))
    status, payload = svc.act(UI, {"handle": "crew-1", "action": "wake", "reason": "owner asked",
                                   "actor": "owner", "actor_source": "webui-session"})
    assert status == 202 and payload["action_id"]
    assert core.audit[-1] == ("crew-1", "wake", "owner", "webui-session")


def test_a_replayed_key_answers_202_and_the_refusal_is_read_as_a_row() -> None:
    """H-1: a duplicate is not a transport error.

    The core records a refused row and returns its id; the UI polls it like every
    other refusal. A 409 would leave the refusal visible only as a status code
    while the row sat unread.
    """
    svc = ManagerService(core=_core(), ui_peers=(UI,), operator_peers=(OP,))
    body = {"handle": "crew-1", "action": "wake", "reason": "r", "actor": "owner",
            "actor_source": "webui-session", "action_id": "k1"}
    first = svc.act(UI, dict(body))
    again = svc.act(UI, dict(body))
    assert first[0] == again[0] == 202 and first[1]["action_id"] == again[1]["action_id"] == "k1"
    conflict = svc.act(UI, {**body, "action": "freeze"})
    assert conflict[0] == 202, "a conflicting replay is a refused row, not a 409"
    assert svc.action(UI, conflict[1]["action_id"])[1]["state"] == "refused"


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    svc = ManagerService(core=_core(), ui_peers=(UI,), operator_peers=(OP,))
    status, payload = svc.act(UI, {"handle": "crew-1", "action": "wake", "reason": "r", "actor": "o",
                                   "actor_source": "terminal", "socket": "/run/podman.sock"})
    assert status == 400 and "unknown field" in payload["detail"]


def test_an_unknown_action_id_is_404_not_an_invented_failure() -> None:
    svc = ManagerService(core=_core(), ui_peers=(UI,), operator_peers=(OP,))
    assert svc.action(UI, "nope")[0] == 404


def test_a_degraded_core_is_503_with_its_own_words() -> None:
    svc = ManagerService(core=_core(unavailable="podman socket unreadable"), ui_peers=(UI,), operator_peers=(OP,))
    status, payload = svc.health()
    assert status == 503 and payload["available"] is False
    assert "unreadable" in payload["detail"]


def test_restart_ui_admits_two_doors_and_exactly_one_class() -> None:
    """L-10's standing (ruled 2026-11-11): the door is not the principal.

    Either admitted door may carry it — the operator's own host, or the UI pod
    carrying that operator's identity — but the actor is an allow-list of exactly
    one class, `host_operator`. A stranger address is not "close enough", and no
    other class (a Lead, a crew agent, a service identity, a session, a terminal)
    gets through even from an admitted door. Every refusal happens before effect:
    no row, no core call, and the class it caught is named.
    """
    core = _core()
    core.agents_by_handle[service.UI_POD] = AgentObserved(handle=service.UI_POD, state="alive")
    svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))

    refused_stranger, payload = svc.restart_ui("10.0.0.99", {"reason": "r", "actor": "evans",
                                                          "actor_source": "host-wg"})
    assert refused_stranger == 403 and "not an admitted door" in payload["detail"]

    for caught in ("webui-session", "channel", "terminal", "lead-1", ""):
        status, payload = svc.restart_ui(UI, {"reason": "r", "actor": "evans",
                                             "actor_source": caught})
        assert status in (400, 403)
        if status == 403:
            assert "provenance is not the host operator" in payload["detail"], payload
            assert repr(caught) in payload["detail"], "the refusal names the class it caught"
        else:
            assert "missing reason, actor or actor_source" in payload["detail"]
    # A name is not optional either (F-7): provenance without a person is anonymous.
    assert svc.restart_ui(OP, {"reason": "r", "actor": "", "actor_source": "host-wg"})[0] == 400
    assert not core.audit, "no refusal above reached the core"


def test_restart_ui_is_accepted_from_either_door_and_is_best_effort() -> None:
    """H-6: 202 means accepted-and-best-effort, never "it worked".

    The replacement kills the process that asked, so a client that died
    mid-request cannot be shown a verdict. The response says so instead of
    implying one, and the action row is the witness — from either door.
    """
    for door in (OP, UI):
        core = _core()
        core.agents_by_handle[service.UI_POD] = AgentObserved(handle=service.UI_POD, state="alive")
        svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))

        status, payload = svc.restart_ui(door, {"reason": "invalidate sessions", "actor": "evans",
                                                "actor_source": "host-wg", "action_id": "r1"})
        assert status == 202
        assert payload["status"] == "accepted" and payload["best_effort"] is True
        assert "succeeded" in payload["note"] and "will not see" in payload["note"], (
            "the contract names what the caller will never observe")
        assert payload["status"] not in ("succeeded", "failed", "timeout")
        # Acceptance (b), as corrected by soro: the row carries the pod's handle, the
        # operator's name, and the door's provenance — all three, or it proved half.
        assert core.audit[-1][0] == service.UI_POD, "the audit row names the pod, not a synthetic handle"
        assert core.audit[-1][1] == "restart-ui"
        assert core.audit[-1][2:] == ("evans", "host-wg"), "name and provenance together"


def test_the_agent_path_does_not_carry_the_pod_verb() -> None:
    """The other half of the same ruling: `act` names the refusal, and the core is never asked."""
    core = _core()
    svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))
    status, payload = svc.act(UI, {"handle": "crew-1", "action": "restart-ui", "reason": "r",
                                   "actor": "evans", "actor_source": "host-wg"})
    assert status == 403 and "host operator's verb" in payload["detail"]
    assert not core.audit, "a closed door leaves no row"


def test_the_row_the_ui_reads_names_who_asked_and_why() -> None:
    """The read half of (c): the post-restart reader can see the audit fields.

    D-12's contradiction, ruled 2026-11-11: the UI is the reader, so the hop must
    expose `actor`, `actor_source`, `reason`, state and detail by action id. This
    is a projection of the row the core already writes — no new verb, no second
    store, and nothing here lets the UI read Manager state directly.
    """
    core = _core()
    core.agents_by_handle[service.UI_POD] = AgentObserved(handle=service.UI_POD, state="alive")
    svc = ManagerService(core=core, ui_peers=(UI,), operator_peers=(OP,))
    status, _ = svc.restart_ui(OP, {"reason": "sessions leaked", "actor": "evans",
                                    "actor_source": "host-wg", "action_id": "restart-7"})
    assert status == 202

    read, payload = svc.action(UI, "restart-7")
    assert read == 200
    assert payload == {"state": "succeeded", "detail": "replaced the UI pod; sessions invalidated",
                       "id": "restart-7", "handle": service.UI_POD, "action": "restart-ui",
                       "actor": "evans", "actor_source": "host-wg", "reason": "sessions leaked"}


def test_the_client_passes_the_audit_fields_through_untouched() -> None:
    answered = {"state": "succeeded", "detail": "replaced the UI pod", "id": "r-1",
                "handle": "babo-ui", "action": "restart-ui", "actor": "evans",
                "actor_source": "host-wg", "reason": "sessions leaked"}

    def transport(method: str, url: str, body: object, timeout_s: float) -> tuple[int, str]:
        return 200, json.dumps(answered)

    result = hop.HopClient("http://x", transport=transport).action("r-1")
    assert result.ok and result.payload == answered
