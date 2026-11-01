"""The web interface's town routes: the same views and decisions as the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest

from babo.baton import growth
from babo.baton.inbox import unread
from babo.baton.town import Town
from babo.webui.town_api import TownRoutes


def _routes(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch, *, authorized: bool = True) -> TownRoutes:
    from babo.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path)}})
    monkeypatch.setattr("babo.config.loader.load_config", lambda *_a, **_k: cfg)
    payloads: dict[int, dict[str, Any]] = {}

    def payload(request: WsRequest) -> dict[str, Any] | None:
        return payloads.get(id(request))

    routes = TownRoutes(authorized=lambda _r: authorized, payload=payload)
    routes.payloads = payloads  # type: ignore[attr-defined]
    return routes


async def _call(routes: TownRoutes, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = WsRequest(path, Headers())
    if body is not None:
        routes.payloads[id(request)] = body  # type: ignore[attr-defined]
    try:
        response = await routes.dispatch(request, path)
    finally:
        # id() values are reused once a request is freed; never let a payload outlive its request.
        routes.payloads.pop(id(request), None)  # type: ignore[attr-defined]
    assert response is not None
    text = response.body.decode() if response.body else ""
    try:
        return response.status_code, json.loads(text)
    except ValueError:
        return response.status_code, text


async def test_overview_and_review_match_the_town(town: Town, index_path: Path, box, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    box("mayor").propose_change("Name the town", "why", "what", town_name="Lantern Hill")
    status, overview = await _call(routes, "/api/town")
    assert status == 200
    assert sorted(r["id"] for r in overview["residents"]) == ["archivist", "inspector", "mayor"]
    assert overview["pending"]["proposals"] == 1 and overview["name"] == ""
    status, review = await _call(routes, "/api/town/review")
    assert status == 200 and review["proposals"][0]["town_name"] == "Lantern Hill"


async def test_deciding_from_the_web_names_the_town_and_tells_the_author(town: Town, index_path: Path, box, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    box("mayor").propose_change("Name the town", "why", "what", town_name="Lantern Hill")
    item = growth.open_proposals(town)[0].path.name
    status, result = await _call(routes, "/api/town/decide", {"kind": "proposal", "id": item, "yes": True, "reason": "lovely"})
    assert status == 200, result
    assert town.name == "Lantern Hill" and result["town"]["name"] == "Lantern Hill"
    assert any("lovely" in i.body for i in unread(town, "mayor"))


async def test_notes_and_refusals(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    status, _ = await _call(routes, "/api/town/note", {"resident": "archivist", "text": "Thanks!"})
    assert status == 200 and [i.sender for i in unread(town, "archivist")] == ["owner"]
    status, text = await _call(routes, "/api/town/decide", {"kind": "proposal", "id": "../../CHARTER.md", "yes": True, "reason": "x"})
    assert status == 400
    status, _ = await _call(routes, "/api/town/decide", {"kind": "nonsense", "id": "x", "yes": True, "reason": "x"})
    assert status == 400
    # A mutation path reached as a plain HTTP request (no WebUI payload) changes nothing.
    status, _ = await _call(routes, "/api/town/name")
    assert status == 405 and town.name == ""


async def test_unauthorized_requests_are_refused(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch, authorized=False)
    status, _ = await _call(routes, "/api/town")
    assert status == 401
    assert await routes.dispatch(WsRequest("/api/other", Headers()), "/api/other") is None


async def test_profiles_from_the_web_match_babo_profile(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    status, body = await _call(routes, "/api/town/profiles")
    assert status == 200
    mayor = body["profiles"][0]
    assert mayor["agent"] == "mayor" and mayor["main"] and mayor["capabilities"][0]["levels"] == ["allow", "deny"]
    status, result = await _call(routes, "/api/town/profiles/update", {"agent": "archivist", "changes": {"web_search": "allow"}})
    assert status == 200, result
    assert result["message"].startswith("Profile of archivist: Search the web: deny → allow")
    archivist = next(p for p in result["profiles"] if p["agent"] == "archivist")
    assert next(c for c in archivist["capabilities"] if c["key"] == "web_search")["level"] == "allow"
    status, text = await _call(routes, "/api/town/profiles/update", {"agent": "mayor", "changes": {"shell": "ask"}})
    assert status == 400 and "can't stop to ask" in str(text)


async def test_upkeep_status_and_catch_up_from_the_web(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import upkeep

    routes = _routes(town, index_path, monkeypatch)
    status, overview = await _call(routes, "/api/town")
    assert status == 200 and overview["upkeep"]["service"] == "not running" and overview["last_upkeep"] is None
    status, result = await _call(routes, "/api/town/upkeep/now", {})
    assert status == 200 and "within a minute" in result["message"]
    assert upkeep.catch_up_requested(town) and result["town"]["upkeep"]["catch_up_requested"]


async def test_sessions_from_the_web_match_babo_session(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from babo.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path),
                                           "opencode": {"bin": str(tmp_path / "no-opencode"), "dataDir": str(tmp_path / "oc")}}})
    routes = _routes(town, index_path, monkeypatch)
    monkeypatch.setattr("babo.config.loader.load_config", lambda *_a, **_k: cfg)
    status, body = await _call(routes, "/api/town/sessions")
    assert status == 200 and body["opencode"] is False
    assert [r["id"] for r in body["residents"]] == ["archivist", "inspector"]  # the Mayor stays in Babo
    status, text = await _call(routes, "/api/town/sessions/new", {"resident": "archivist", "title": "tidy"})
    assert status == 400 and "opencode is not installed" in str(text)
    status, text = await _call(routes, "/api/town/sessions/new", {"resident": "mayor"})
    assert status == 400 and "runs in Babo itself" in str(text)


async def test_an_unexpected_failure_says_what_broke_and_what_to_do(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise TypeError("town_overview() missing 1 required positional argument: 'cfg'")

    monkeypatch.setattr("babo.baton.views.town_overview", broken)
    status, text = await _call(routes, "/api/town")
    assert status == 500 and "TypeError" in str(text) and "systemctl --user restart babo-queue babo-gateway babo-upkeep" in str(text)


async def test_a_session_request_is_approved_from_the_web_only_for_what_was_shown(town: Town, index_path: Path, box, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import requests

    routes = _routes(town, index_path, monkeypatch)
    box("mayor").call("send_errand", resident="archivist", task="Install nano.", reason="To edit notes.", commands=["sudo pacman -S nano"])
    status, review = await _call(routes, "/api/town/review")
    [shown] = review["sessions"]
    assert status == 200 and shown["commands"] == ["sudo pacman -S nano"]
    status, text = await _call(routes, "/api/town/decide", {"kind": "session", "id": shown["id"], "yes": True, "reason": "ok"})
    assert status == 400 and "missing digest" in str(text)
    status, result = await _call(routes, "/api/town/decide", {"kind": "session", "id": shown["id"], "yes": True, "reason": "ok",
                                                              "digest": shown["digest"]})
    assert status == 200 and "approved" in result["message"]
    assert requests.load(town, shown["id"]).status == "approved"


async def test_a_chat_asks_for_waiting_session_requests_to_prompt_for_them(town: Town, index_path: Path, box, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    status, body = await _call(routes, "/api/town/session-requests")
    assert status == 200 and body["pending"] == []
    box("mayor").call("send_errand", resident="archivist", task="Install nano.", reason="To edit notes.", commands=["sudo pacman -S nano"])
    status, body = await _call(routes, "/api/town/session-requests")
    [shown] = body["pending"]
    assert shown["resident"] == "archivist" and shown["digest"] and shown["wider"] == ["Commands: sudo pacman -S nano"]
    await _call(routes, "/api/town/decide", {"kind": "session", "id": shown["id"], "yes": False, "reason": "not now", "digest": shown["digest"]})
    status, body = await _call(routes, "/api/town/session-requests")
    assert body["pending"] == []


async def test_allow_all_from_the_web_matches_babo_allow_all(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import allow_all

    routes = _routes(town, index_path, monkeypatch)
    status, body = await _call(routes, "/api/town/allow-all/update", {"key": "websocket:abc", "on": True, "hours": 1})
    assert status == 200 and "websocket:abc" in body["active"] and allow_all.is_on(town, "websocket:abc")
    status, body = await _call(routes, "/api/town/allow-all")
    assert status == 200 and body["active"]["websocket:abc"]["until"]
    status, text = await _call(routes, "/api/town/allow-all/update", {"key": "websocket:abc", "on": True, "hours": "lots"})
    assert status == 400
    status, body = await _call(routes, "/api/town/allow-all/update", {"key": "websocket:abc", "on": False})
    assert status == 200 and body["active"] == {}
