"""The office routes: a live view, checked changes, and one honest lifecycle verb."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest

from babo.baton import projects as project_board
from babo.baton.town import Town
from babo.office.model import AgentObserved
from babo.webui import office_api
from baton.office_fakes import FakeCore


def _routes(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch, *,
            manager: FakeCore | None = None, authorized: bool = True) -> office_api.OfficeRoutes:
    from babo.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path),
                                          "office": {"backend": "stub"}}})
    monkeypatch.setattr("babo.config.loader.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(office_api, "_OPS", None)
    backend = manager or FakeCore()
    monkeypatch.setattr(office_api, "_manager", lambda: backend)
    payloads: dict[int, dict[str, Any]] = {}

    def payload(request: WsRequest) -> dict[str, Any] | None:
        return payloads.get(id(request))

    routes = office_api.OfficeRoutes(authorized=lambda _r: authorized, payload=payload)
    routes.payloads = payloads  # type: ignore[attr-defined]
    routes.manager = backend  # type: ignore[attr-defined]
    return routes


async def _call(routes: office_api.OfficeRoutes, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = WsRequest(path, Headers())
    if body is not None:
        routes.payloads[id(request)] = body  # type: ignore[attr-defined]
    try:
        response = await routes.dispatch(request, path)
    finally:
        routes.payloads.pop(id(request), None)  # type: ignore[attr-defined]
    assert response is not None
    text = response.body.decode() if response.body else ""
    try:
        return response.status_code, json.loads(text)
    except ValueError:
        return response.status_code, text


def _project(town: Town, title: str = "Control plane") -> str:
    return project_board.new_project(town, "mayor", title).slug


async def _settle(routes: office_api.OfficeRoutes, op_id: str) -> dict[str, Any]:
    for _ in range(200):
        _status, op = await _call(routes, f"/api/office/ops/{op_id}")
        if op["state"] != "pending" and op["state"] != "running":
            return op
        await asyncio.sleep(0.01)
    raise AssertionError("the action never settled")


async def test_the_view_joins_records_and_shows_what_is_missing(town: Town, index_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    routes = _routes(town, index_path, monkeypatch)
    status, body = await _call(routes, "/api/office/agents/record",
                              {"handle": "crew-1", "resident": "archivist", "project": slug})
    assert status == 200 and "crew-1" in body["message"]

    status, view = await _call(routes, "/api/office")
    assert status == 200
    assert view["manager"] == {"name": "fake", "available": True, "said": ""}
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["status"] == "missing" and agent["desired"] == "running"
    assert view["missing"] == ["crew-1"]
    assert next(p for p in view["projects"] if p["slug"] == slug)["agents"] == ["crew-1"]


async def test_waking_an_agent_is_pending_then_the_managers_answer(town: Town, index_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    manager = FakeCore(agents_by_handle={"crew-1": AgentObserved(handle="crew-1", state="frozen")})
    routes = _routes(town, index_path, monkeypatch, manager=manager)
    await _call(routes, "/api/office/agents/record",
                {"handle": "crew-1", "resident": "archivist", "project": slug, "desired": "frozen"})

    status, started = await _call(routes, "/api/office/agents/wake", {"handle": "crew-1"})
    assert status == 202 and started["state"] == "pending"
    op = await _settle(routes, started["op"]["id"])
    assert op["state"] == "succeeded"
    assert op["observed"]["state"] == "alive"

    _status, view = await _call(routes, "/api/office")
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["status"] == "running" and agent["desired"] == "running"


async def test_freezing_says_released_not_paused_and_not_saved(town: Town, index_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    manager = FakeCore(agents_by_handle={"crew-1": AgentObserved(handle="crew-1", state="alive")})
    routes = _routes(town, index_path, monkeypatch, manager=manager)
    await _call(routes, "/api/office/agents/record", {"handle": "crew-1", "resident": "archivist", "project": slug})
    _status, started = await _call(routes, "/api/office/agents/freeze", {"handle": "crew-1"})
    op = await _settle(routes, started["op"]["id"])
    assert op["state"] == "succeeded" and "pod released" in op["said"] and "paused" not in op["said"]
    _status, view = await _call(routes, "/api/office")
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["status"] == "frozen"


async def test_a_failed_action_says_why_and_leaves_the_record_alone(town: Town, index_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    manager = FakeCore(agents_by_handle={"crew-1": AgentObserved(handle="crew-1", state="frozen")}, fail={"wake"})
    routes = _routes(town, index_path, monkeypatch, manager=manager)
    await _call(routes, "/api/office/agents/record",
                {"handle": "crew-1", "resident": "archivist", "project": slug, "desired": "frozen"})
    _status, started = await _call(routes, "/api/office/agents/wake", {"handle": "crew-1"})
    op = await _settle(routes, started["op"]["id"])
    assert op["state"] == "failed" and "failed at the runtime" in op["said"]
    _status, view = await _call(routes, "/api/office")
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["desired"] == "frozen" and agent["status"] == "frozen"


async def test_an_unrecorded_agent_is_refused_by_the_core_not_by_the_page(town: Town, index_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """One refusal, one place: the page asks, the core declines by name (L-02/L-03)."""
    routes = _routes(town, index_path, monkeypatch)
    status, started = await _call(routes, "/api/office/agents/wake", {"handle": "ghost"})
    assert status == 202
    op = await _settle(routes, started["op"]["id"])
    assert op["state"] == "refused" and "record it first" in op["said"]


async def test_an_unreachable_manager_is_shown_as_unknown_not_running(town: Town, index_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    manager = FakeCore(unavailable="the manager core is unavailable")
    routes = _routes(town, index_path, monkeypatch, manager=manager)
    await _call(routes, "/api/office/agents/record", {"handle": "crew-1", "resident": "archivist", "project": slug})
    status, view = await _call(routes, "/api/office")
    assert status == 200
    assert view["manager"]["available"] is False
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["status"] == "unknown" and agent["desired"] == "running"


async def test_crew_changes_go_through_the_same_checks(town: Town, index_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    slug = _project(town)
    routes = _routes(town, index_path, monkeypatch)
    status, body = await _call(routes, "/api/office/crew", {"project": slug, "lead": "crew-1", "crew": ["crew-2"]})
    assert status == 200 and body["crew"]["lead"] == "crew-1"
    status, _body = await _call(routes, "/api/office/crew", {"project": slug, "crew": ["Not A Handle"]})
    assert status == 400


async def test_unauthorized_reads_are_refused(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch, authorized=False)
    status, _body = await _call(routes, "/api/office")
    assert status == 401


async def test_the_office_view_reports_the_ui_listener_not_the_health_port(town: Town, index_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _routes(town, index_path, monkeypatch)
    from babo.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path),
                                          "office": {"backend": "stub"}},
                                 "gateway": {"host": "10.0.0.11", "port": 18790},
                                 "channels": {"websocket": {"host": "10.0.0.11", "port": 8843}}})
    monkeypatch.setattr("babo.config.loader.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(office_api, "_ops", lambda: __import__("babo.office.control", fromlist=["Ops"]).Ops())
    status, view = await _call(routes, "/api/office")
    assert status == 200
    assert view["listener"] == "10.0.0.11:8843", "the UI port, never the health port"


async def test_before_the_core_lands_the_office_says_so(town: Town, index_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """No core, no pretending: desired records are listed, agents are `unknown`,
    and the banner names the reason. A stub here would be a second core."""
    from babo.office.api import CoreNotBuiltError

    slug = _project(town)
    routes = _routes(town, index_path, monkeypatch)
    await _call(routes, "/api/office/agents/record", {"handle": "crew-1", "resident": "archivist", "project": slug})

    def unbuilt(_cfg: object = None) -> object:
        raise CoreNotBuiltError("the native manager core hasn't landed yet")

    monkeypatch.setattr(office_api, "_manager", unbuilt)
    status, view = await _call(routes, "/api/office")
    assert status == 200
    assert view["manager"]["available"] is False and "hasn't landed" in view["manager"]["said"]
    agent = next(a for a in view["agents"] if a["handle"] == "crew-1")
    assert agent["status"] == "unknown" and agent["desired"] == "running"
    assert agent["said"] == view["manager"]["said"]


async def test_the_other_three_verbs_are_reachable_and_report_the_cores_word(town: Town, index_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn/rescue/destroy ride the same pending→verdict path; no friendlier synonyms."""
    slug = _project(town)
    manager = FakeCore(agents_by_handle={"crew-1": AgentObserved(handle="crew-1", state="frozen")})
    routes = _routes(town, index_path, monkeypatch, manager=manager)
    await _call(routes, "/api/office/agents/record", {"handle": "crew-1", "resident": "archivist", "project": slug})
    for verb in ("spawn", "rescue", "destroy"):
        status, started = await _call(routes, f"/api/office/agents/{verb}", {"handle": "crew-1"})
        assert status == 202, verb
        op = await _settle(routes, started["op"]["id"])
        assert op["state"] == "refused", verb
        assert verb in op["said"]
