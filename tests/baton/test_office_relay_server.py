"""The relay's glue: five routes, no judgement, and no default anything.

The rows are in two halves. The first half is about *starting*: there is no default
bind and no default door, and both refusals name the value that is missing, because
the wrong defaults are the two things this house has already ruled out. The second
half is about *adding nothing*: every body the glue returns must be exactly what
`relay.py` computed, in the same shape, with 200 — including the refusals.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from babo.office import relay_server
from babo.office.relay import RELAY_PORT, Relay

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - aiohttp is a runtime dependency
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)
pytestmark = pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp test utils unavailable")


class FakeDoor:
    def __init__(self, *answers: tuple[int, str] | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, body: dict[str, Any] | None,
                 timeout_s: float) -> tuple[int, str]:
        self.calls.append({"method": method, "url": url, "body": body})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


# -- starting: no defaults ----------------------------------------------------
def test_there_is_no_default_bind() -> None:
    with pytest.raises(relay_server.RelayConfigError) as refused:
        relay_server.bind_address({})
    sentence = str(refused.value)
    assert "BABO_OFFICE_BIND" in sentence
    assert "0.0.0.0" in sentence and "loopback" in sentence


def test_the_bind_is_the_value_it_was_given_and_nothing_else() -> None:
    assert relay_server.bind_address({"BABO_OFFICE_BIND": "10.91.0.7"}) == "10.91.0.7"


def test_a_blank_bind_is_missing_rather_than_empty() -> None:
    with pytest.raises(relay_server.RelayConfigError):
        relay_server.bind_address({"BABO_OFFICE_BIND": "   "})


def test_the_door_must_be_named_too() -> None:
    with pytest.raises(relay_server.RelayConfigError) as refused:
        relay_server.door_url({"BABO_OFFICE_BIND": "10.91.0.7"})
    assert "BABO_OFFICE_DOOR" in str(refused.value)


def test_the_port_is_the_one_constant_the_manager_reads() -> None:
    assert relay_server.RELAY_PORT is RELAY_PORT


# -- answering: the glue adds nothing ----------------------------------------
@pytest_asyncio.fixture
async def client():
    opened: list[TestClient] = []

    async def _open(*answers: tuple[int, str] | BaseException):
        door = FakeDoor(*answers)
        app = relay_server.create_app(Relay("http://127.0.0.1:8900", transport=door))
        client = TestClient(TestServer(app))
        await client.start_server()
        opened.append(client)
        return client, door

    try:
        yield _open
    finally:
        for client in opened:
            await client.close()


@pytest.mark.asyncio
async def test_health_answers_before_anything_else_is_ready(client) -> None:
    """No door is asked, so a deaf agent cannot make the relay look down."""
    api, door = await client(OSError("connection refused"))
    response = await api.get("/health")
    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True and body["relay"]["version"] == "1"
    assert door.calls == []


@pytest.mark.asyncio
async def test_a_deaf_agent_is_200_unknown_and_not_a_500(client) -> None:
    api, _ = await client(OSError("connection refused"))
    response = await api.get("/status")
    assert response.status == 200
    body = await response.json()
    assert set(body) == {"unknown"} and body["unknown"].startswith("status: ")
    assert "in_flight" not in body


@pytest.mark.asyncio
async def test_the_position_passes_through_untouched(client) -> None:
    answer = ('{"in_flight": true, "count": 1, "turns": [{"session": "api:cli", '
              '"turn_id": "t-1", "wall_started_at": "2026-11-15T12:00:00Z"}]}')
    api, _ = await client((200, answer))
    body = await (await api.get("/status")).json()
    assert body == {"seen": True, "in_flight": True, "count": 1,
                    "turns": [{"session": "api:cli", "turn_id": "t-1",
                               "wall_started_at": "2026-11-15T12:00:00Z"}]}


@pytest.mark.asyncio
async def test_the_reason_reaches_the_agent(client) -> None:
    api, door = await client((200, '{"accepted": true, "in_flight": false, "count": 0, "turns": []}'))
    await api.post("/quiesce", json={"reason": "sessions leaked"})
    assert door.calls[0]["body"] == {"reason": "sessions leaked"}


@pytest.mark.asyncio
async def test_an_agents_sentence_passes_through_as_a_refusal(client) -> None:
    api, _ = await client((409, '{"detail": "a turn is mid-tool-call"}'))
    response = await api.post("/quiesce", json={"reason": "x"})
    assert response.status == 200
    assert await response.json() == {"detail": "a turn is mid-tool-call"}


@pytest.mark.asyncio
async def test_message_carries_what_the_caller_sent(client) -> None:
    api, door = await client((200, "{}"))
    response = await api.post("/message", json={
        "from": "@evans:matrix.home", "channel": "matrix", "content": "hello",
        "attachments": [],
    })
    assert await response.json() == {"delivered": True}
    assert door.calls[0]["body"] == {"from": "@evans:matrix.home", "channel": "matrix",
                                     "content": "hello", "attachments": []}


@pytest.mark.asyncio
async def test_an_attachment_is_refused_without_calling_the_door(client) -> None:
    api, door = await client()
    body = await (await api.post("/message", json={
        "from": "s", "channel": "c", "content": "x", "attachments": ["cat.png"],
    })).json()
    assert set(body) == {"unknown"} and "attachments" in body["unknown"]
    assert door.calls == []


@pytest.mark.asyncio
async def test_the_glue_returns_200_for_every_shape_it_can_produce(client) -> None:
    """The status code is a claim about this hop, and this hop answered."""
    api, _ = await client(OSError("connection refused"), OSError("connection refused"),
                          OSError("connection refused"), OSError("connection refused"))
    for method, path, payload in (
        ("get", "/status", None),
        ("post", "/quiesce", {"reason": "x"}),
        ("post", "/flush", {"reason": "x"}),
        ("post", "/message", {"from": "s", "channel": "c", "content": "x"}),
    ):
        response = await getattr(api, method)(path, json=payload) if payload else await getattr(api, method)(path)
        assert response.status == 200, path
        assert "unknown" in await response.json(), path
