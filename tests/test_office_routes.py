"""The office door: the four routes the relay knocks on.

These rows are the wire shapes of `docs/OFFICE-RELAY.md`, driven through a real
aiohttp app. They do not re-test the relay — `tests/baton/test_office_relay.py`
owns that side — they test that the agent answers in the shape the relay expects,
and that the door is reachable from where the relay stands (loopback, in the pod,
without the API key).
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from babo.agent.loop import Quiesced, TurnRecord
from babo.api.server import create_app

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - aiohttp is a runtime dependency
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)
pytestmark = pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp test utils unavailable")

API_KEY = "secret"


class FakeLoop:
    """The parts of an AgentLoop the door touches, and nothing else."""

    provider = object()

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self.refusal: Quiesced | None = None
        self.accepted: list[dict[str, str]] = []
        self.flushes = 0

    def active_turns(self) -> list[dict[str, Any]]:
        return list(self.turns)

    @property
    def quiesced(self) -> Quiesced | None:
        return self.refusal

    def quiesce(self, reason: str) -> list[dict[str, Any]]:
        if self.refusal is None:
            self.refusal = Quiesced(f"the agent is not admitting new work ({reason})", reason)
        return self.active_turns()

    async def office_flush(self) -> dict[str, Any]:
        self.flushes += 1
        return {"persisted": True, "detail": "the store committed every accepted operation"}

    async def office_accept(self, *, source: str, channel: str, content: str) -> bool:
        if self.refusal is not None:
            return False
        self.accepted.append({"from": source, "channel": channel, "content": content})
        return True


@pytest.fixture
def loop() -> FakeLoop:
    return FakeLoop()


@pytest.fixture
def app(loop: FakeLoop):
    return create_app(loop, model_name="test-model", request_timeout=10.0, api_key=API_KEY)


@pytest_asyncio.fixture
async def client(app):
    async with TestClient(TestServer(app)) as client:
        yield client


# -- status -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_says_seen_and_nothing_else_when_no_turn_runs(client) -> None:
    response = await client.get("/v1/office/status")
    assert response.status == 200
    assert await response.json() == {"seen": True, "in_flight": False, "count": 0, "turns": []}


@pytest.mark.asyncio
async def test_status_reports_every_turn_in_flight(client, loop: FakeLoop) -> None:
    loop.turns = [
        TurnRecord("api:matrix:@evans", "t-9f21", 1763201400.0).as_wire(),
        TurnRecord("api:cli@z620", "t-2b07", 1763201464.0).as_wire(),
    ]
    body = await (await client.get("/v1/office/status")).json()
    assert body["in_flight"] is True and body["count"] == 2
    assert [turn["session"] for turn in body["turns"]] == ["api:matrix:@evans", "api:cli@z620"]


# -- quiesce ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quiesce_says_where_the_turns_stand(client) -> None:
    response = await client.post("/v1/office/quiesce", json={"reason": "sessions leaked"})
    assert response.status == 200
    body = await response.json()
    assert body["accepted"] is True and body["count"] == 0 and body["turns"] == []
    assert "quiesced" not in body


@pytest.mark.asyncio
async def test_quiesce_stops_the_chat_door_rather_than_answering_empty(client) -> None:
    """A 200 with no content would be a green with nothing behind it."""
    await client.post("/v1/office/quiesce", json={"reason": "sessions leaked"})
    response = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}],
    }, headers={"Authorization": f"Bearer {API_KEY}"})
    assert response.status == 503
    # The OpenAI door keeps its own envelope; this row is about the refusal
    # happening at all rather than a 200 with an empty completion.
    assert "not admitting new work" in (await response.json())["error"]["message"]


# -- flush --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flush_reports_the_stores_confirmation(client, loop: FakeLoop) -> None:
    body = await (await client.post("/v1/office/flush", json={"reason": "stand down"})).json()
    assert body == {"persisted": True, "detail": "the store committed every accepted operation"}
    assert loop.flushes == 1


# -- message ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_message_is_accepted_and_returns_before_the_turn(client, loop: FakeLoop) -> None:
    response = await client.post("/v1/office/message", json={
        "from": "@evans:matrix.home", "channel": "matrix", "content": "hello",
        "attachments": [],
    })
    assert response.status == 200
    assert await response.json() == {"accepted": True}
    assert loop.accepted == [{"from": "@evans:matrix.home", "channel": "matrix", "content": "hello"}]


@pytest.mark.asyncio
async def test_message_refuses_attachments_by_name(client, loop: FakeLoop) -> None:
    response = await client.post("/v1/office/message", json={
        "from": "@evans:matrix.home", "channel": "matrix", "content": "hi",
        "attachments": ["cat.png"],
    })
    assert response.status == 400
    assert "attachments" in (await response.json())["detail"]
    assert loop.accepted == [], "nothing may be accepted while part of it is dropped"


@pytest.mark.asyncio
async def test_message_while_quiesced_is_a_refusal_not_an_acceptance(client, loop: FakeLoop) -> None:
    await client.post("/v1/office/quiesce", json={"reason": "stand down"})
    response = await client.post("/v1/office/message", json={
        "from": "@evans:matrix.home", "channel": "matrix", "content": "hi",
    })
    assert response.status == 409
    assert "not admitting work" in (await response.json())["detail"]
    assert loop.accepted == []


@pytest.mark.asyncio
async def test_message_without_its_identity_is_refused(client) -> None:
    response = await client.post("/v1/office/message", json={"content": "hi"})
    assert response.status == 400
    assert "from, channel and content" in (await response.json())["detail"]


# -- the door the relay actually stands at ------------------------------------
@pytest.mark.asyncio
async def test_the_office_door_answers_without_the_api_key_from_loopback(client) -> None:
    """The relay is in this pod and reaches the agent on loopback, so it needs no key."""
    response = await client.get("/v1/office/status")  # no Authorization header
    assert response.status == 200


@pytest.mark.asyncio
async def test_the_chat_door_still_needs_the_key(client) -> None:
    """Only the office door is exempt; the API surface keeps its auth."""
    response = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status == 401
