"""Upkeep: memory reviews, curation and inspection whenever they are due, never in anyone's way."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from babo.baton import upkeep, views
from babo.baton.config import BatonConfig
from babo.baton.events import append_event
from babo.baton.town import Town
from baton.mock_llama import MockLlama, schema
from baton.test_night import FOLD, _a_day_in_town, _night_chats, _router

NOON = datetime(2026, 10, 29, 12, 0).astimezone()


def _cfg(town: Town, **upkeep_cfg: Any) -> BatonConfig:
    return BatonConfig.model_validate({"town": str(town.root), "upkeep": upkeep_cfg})


def _work(town: Town, resident: str = "mayor", *, at: datetime) -> None:
    for kind in ("turn_start", "tool"):
        append_event(town, resident, "cli:day", kind, tool="read_file")
    # Backdate the activity: the event log's times are what upkeep reads.
    for path in (town.resident_dir(resident) / "sessions").rglob("*.events.jsonl"):
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        path.write_text("".join(json.dumps({**e, "at": at.isoformat(timespec="seconds")}) + "\n" for e in lines))


def test_quiet_hours_wrap_past_midnight() -> None:
    assert upkeep.in_quiet_hours(NOON.replace(hour=2), "01:00-06:00")
    assert not upkeep.in_quiet_hours(NOON, "01:00-06:00")
    assert upkeep.in_quiet_hours(NOON.replace(hour=23, minute=30), "22:00-06:00")
    assert upkeep.in_quiet_hours(NOON.replace(hour=3), "22:00-06:00")


def test_a_long_session_is_reviewed_once_it_goes_quiet_and_nobody_needs_the_model(town: Town) -> None:
    cfg = _cfg(town)
    _work(town, at=NOON - timedelta(hours=5))
    plan = upkeep.due(town, cfg, now=NOON, model_idle_s=3600)
    assert plan.run and plan.residents == ["mayor"] and plan.reasons == ["mayor: 5 hours of activity to review"]
    busy = upkeep.due(town, cfg, now=NOON, model_idle_s=30)
    assert not busy.run and busy.waiting_for == "someone used the model in the last 10 minutes"
    assert upkeep.due(town, cfg, now=NOON, model_idle_s=None).waiting_for == "the model queue isn't answering"


def test_recent_activity_waits_for_its_session_to_end_except_in_quiet_hours(town: Town) -> None:
    cfg = _cfg(town)
    _work(town, at=NOON - timedelta(minutes=5))
    assert upkeep.due(town, cfg, now=NOON, model_idle_s=3600).reasons == []  # still working
    _work(town, at=NOON - timedelta(hours=1))
    assert upkeep.due(town, cfg, now=NOON, model_idle_s=3600).reasons == []  # quiet, but only an hour of it
    night = NOON.replace(hour=2)
    _work(town, at=night - timedelta(hours=1))
    assert upkeep.due(town, cfg, now=night, model_idle_s=3600).residents == ["mayor"]


def test_notes_to_curate_are_due_without_any_conversation(town: Town) -> None:
    (town.promotions / "2026-10-29-mayor-tomatoes.md").write_text("---\nfrom: mayor\ntitle: Tomatoes\nstatus: pending\n---\n\nRobin likes tomatoes.\n")
    plan = upkeep.due(town, _cfg(town), now=NOON, model_idle_s=3600)
    assert plan.run and plan.reasons == ["The night round: 1 note(s) to curate into the commons"] and plan.residents == []


def test_unfinished_work_is_not_retried_at_once_but_catch_up_always_runs(town: Town) -> None:
    cfg = _cfg(town)
    tool = town.tools / "pending" / "shout"
    tool.mkdir(parents=True)
    plan = upkeep.due(town, cfg, now=NOON, model_idle_s=3600)
    assert plan.run
    (town.state / "upkeep").mkdir(parents=True, exist_ok=True)
    (town.state / "upkeep" / "scheduler.json").write_text(json.dumps({"last_tried": NOON.isoformat(), "last_keys": plan.keys}))
    later = upkeep.due(town, cfg, now=NOON + timedelta(hours=1), model_idle_s=3600)
    assert not later.run and "trying again after 6 hours" in later.waiting_for
    assert upkeep.due(town, cfg, now=NOON + timedelta(hours=7), model_idle_s=3600).run
    assert upkeep.due(town, cfg, now=NOON + timedelta(hours=1), model_idle_s=0, catch_up=True).run


def test_housekeeping_runs_daily_in_quiet_hours_even_when_nobody_talked(town: Town) -> None:
    cfg = _cfg(town)
    assert upkeep.due(town, cfg, now=NOON, model_idle_s=3600).reasons == []
    night = NOON.replace(hour=3)
    assert upkeep.due(town, cfg, now=night, model_idle_s=3600).reasons == ["daily housekeeping"]
    (town.state / "upkeep").mkdir(parents=True, exist_ok=True)
    (town.state / "upkeep" / "scheduler.json").write_text(json.dumps({"last_pass": (night - timedelta(hours=2)).isoformat()}))
    assert upkeep.due(town, cfg, now=night, model_idle_s=3600).reasons == []


async def test_catch_up_reviews_everything_waiting_and_waits_behind_everyone(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    await _a_day_in_town(make_loop, mock_llama, town)
    mock_llama.default = _router(_night_chats(), folds=[schema(FOLD)])
    asked_before = len(mock_llama.request_headers)
    result = await upkeep.run_now(make_config("mayor"), prefer_container=False)
    assert result is not None and result["status"] == "ok" and result["ran"] == ["mayor"]
    log = subprocess.run(["git", "-C", str(town.root), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("Upkeep ") and "1 memory review" in log
    # Every model call upkeep made went in at the lowest priority.
    upkeep_calls = mock_llama.request_headers[asked_before:]
    priorities = [{k.lower(): v for k, v in h.items()}.get("x-baton-priority") for h in upkeep_calls]
    assert upkeep_calls and priorities == ["upkeep"] * len(upkeep_calls)
    # Reviewed activity is not reviewed again; the status says what happened.
    assert upkeep.due(town, make_config("mayor").baton, now=datetime.now().astimezone() + timedelta(hours=9), model_idle_s=3600).residents == []
    info = upkeep.status(town, make_config("mayor").baton)
    assert info["last_pass"] and info["last_reviews"]["mayor"]
    assert views.last_upkeep(town)["finished"] is True  # type: ignore[index]
    assert await upkeep.run_now(make_config("mayor"), prefer_container=False) is None


def test_the_web_button_asks_the_running_service(town: Town) -> None:
    assert not upkeep.catch_up_requested(town)
    message = views.request_upkeep(town)
    assert "within a minute" in message and upkeep.catch_up_requested(town)
    # The request lives in state/runtime, which the town repo ignores.
    status = subprocess.run(["git", "-C", str(town.root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "upkeep-request" not in status


async def test_the_queue_says_how_long_only_upkeep_has_used_the_model(mock_llama: MockLlama, tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from babo.baton.config import BatonModelConfig
    from babo.baton.queue import ModelQueue

    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url))
    queue.last_busy -= 600
    body = json.dumps({"model": "m", "messages": []})
    async with TestClient(TestServer(queue.app())) as client:
        await client.post("/v1/chat/completions", data=body, headers={"X-Baton-Priority": "upkeep"})
        assert (await (await client.get("/baton/queue")).json())["idle_s"] >= 600
        await client.post("/v1/chat/completions", data=body)
        assert (await (await client.get("/baton/queue")).json())["idle_s"] < 5


@pytest.mark.parametrize("name", ["2026-10-29T14-05", "2026-01-02"])
def test_pass_names_read_plainly(name: str) -> None:
    from babo.baton.night import pass_label

    assert pass_label(name) in ("2026-10-29 14:05", "2026-01-02")
