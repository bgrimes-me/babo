"""Rounds: the town's work, in readable files; upkeep is Babo's own work."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from babo.baton import notices, rounds
from babo.baton.registry import Registry
from babo.baton.town import Town


def _write(town: Town, name: str, text: str) -> None:
    (rounds.folder(town) / f"{name}.md").write_text(text, encoding="utf-8")


PAPER = """---
title: The paper round
when: {event: checkpoint}
steps:
  - office: archivist
    duty: Bring the commons up to date with what was learned.
  - office: reporter
    duty: Draft the next edition from the step before.
    material: previous
  - office: inspector
    duty: Check the draft for anything private.
    material: previous
---
After a checkpoint: the commons, then the paper.
"""


class Ctx:
    def __init__(self, town: Town, fail: str = "") -> None:
        self.town, self.work_dir, self.fail = town, town.state / "upkeep" / "test", fail
        self.turns: list[tuple[str, str]] = []
        self.sessions: list[tuple[str, dict[str, str], list[str]]] = []
        self.lines: list[str] = []

    async def agent_turn(self, resident: str, message: str, session_key: str, kind: str = "upkeep",
                         access: dict[str, str] | None = None, commands: list[str] | None = None) -> str:
        self.turns.append((resident, message))
        self.sessions.append((kind, access or {}, commands or []))
        if resident == self.fail:
            raise RuntimeError("the model wasn't reachable")
        return f"{resident} did its part."

    def log(self, line: str) -> None:
        self.lines.append(line)


def test_the_baseline_town_ships_the_night_round(town: Town) -> None:
    [night] = [r for r in rounds.load(town)[0] if r.name == "night"]
    assert night.when == {"model_free": True} and [s.office for s in night.steps] == ["archivist", "inspector"]
    assert night.steps[0].fallback == "mayor" and "{{ owner }}" not in night.about


def test_round_files_say_plainly_what_is_wrong(town: Town) -> None:
    _write(town, "broken", "---\ntitle: Broken\nwhen: {weekly: someday 9:00}\nsteps:\n  - office: archivist\n    duty: x\n---\n")
    _write(town, "empty", "---\ntitle: Empty\n---\n")
    _, broken = rounds.load(town)
    assert broken == {"broken": "`weekly` takes a day and a time, like monday 08:00", "empty": "a round has at least one step"}


def test_steps_run_in_order_with_the_report_before_and_a_vacancy_waits(town: Town) -> None:
    _write(town, "paper", PAPER)
    ctx = Ctx(town)
    record = asyncio.run(rounds.run(ctx, rounds.get(town, "paper"), trigger="a checkpoint is filed", session_key="t"))
    assert [(s.office, s.status) for s in record.steps] == [("archivist", "done"), ("reporter", "skipped"), ("inspector", "done")]
    assert record.steps[1].note == "vacancy: nobody holds this office"
    # The inspector gets the report of the last step that ran (the reporter's was skipped).
    assert "archivist did its part." in ctx.turns[-1][1] and "Check the draft" in ctx.turns[-1][1]
    [run] = rounds.runs(town, "paper")
    assert run["status"] == "done" and rounds.load_state(town)["paper"]["last_status"] == "done"


def test_a_step_that_fails_stops_the_round_and_the_owner_hears(town: Town) -> None:
    _write(town, "paper", PAPER)
    record = asyncio.run(rounds.run(Ctx(town, fail="archivist"), rounds.get(town, "paper"), trigger="by hand", session_key="t"))
    assert record.status == "failed" and len(record.steps) == 1
    [note] = [n for n in notices.recent(town) if n["kind"] == "round_failed"]
    assert "“The paper round” stopped at step 1 (archivist)" in note["text"]


def test_the_mayor_curates_while_there_is_no_archivist(town: Town) -> None:
    Registry(town).update("archivist", status="retired")
    (town.promotions / "2026-11-01-mayor-soil.md").write_text("---\nfrom: mayor\ntitle: Soil\nstatus: pending\n---\n\nLoam.\n")
    ctx = Ctx(town)
    record = asyncio.run(rounds.run(ctx, rounds.get(town, "night"), trigger="t", session_key="t"))
    assert record.steps[0].resident == "mayor" and "Soil" in ctx.turns[0][1]
    assert "status: reviewed" in (town.promotions / "2026-11-01-mayor-soil.md").read_text()  # the queue moved on


def test_when_rounds_are_due(town: Town) -> None:
    now = datetime(2026, 11, 2, 9, 0).astimezone()
    _write(town, "morning", "---\ntitle: Morning\nwhen: {daily: '08:00'}\nsteps:\n  - office: mayor\n    duty: Read the board.\n---\n")
    _write(town, "after", "---\ntitle: After\nwhen: {after: morning}\nsteps:\n  - office: mayor\n    duty: Then this.\n---\n")
    assert rounds.due(town, now=now - timedelta(days=1)) == []  # first seen: counting starts now
    assert [r.name for r, _ in rounds.due(town, now=now)] == ["morning"]
    asyncio.run(rounds.run(Ctx(town), rounds.get(town, "morning"), trigger="t", session_key="t"))
    assert [r.name for r, _ in rounds.due(town, now=datetime.now().astimezone())] == ["after"]
    assert rounds.next_scheduled(rounds.get(town, "morning"), now).hour == 8
    weekly = rounds.parse("w", "---\ntitle: W\nwhen: {weekly: monday 08:00}\nsteps:\n  - office: mayor\n    duty: x\n---\n")
    assert rounds.next_scheduled(weekly, now).weekday() == 0 and weekly.describe_when() == "every Monday at 08:00"


def test_the_night_round_is_due_when_notes_wait_and_upkeep_runs_it(town: Town) -> None:
    from babo.baton import upkeep
    from babo.baton.config import BatonConfig

    cfg = BatonConfig(town=str(town.root))
    (town.promotions / "2026-11-01-mayor-soil.md").write_text("---\nfrom: mayor\ntitle: Soil\nstatus: pending\n---\n\nLoam.\n")
    plan = upkeep.due(town, cfg, now=datetime.now().astimezone(), model_idle_s=0)
    assert plan.rounds == [("night", "The night round: 1 note(s) to curate into the commons")]
    assert plan.waiting_for.startswith("someone used the model")  # it waits its turn
    rounds.set_options(town, "night", runs="now")
    assert upkeep.due(town, cfg, now=datetime.now().astimezone(), model_idle_s=0).run  # runs when triggered
    rounds.set_options(town, "night", enabled=False)
    assert upkeep.due(town, cfg, now=datetime.now().astimezone(), model_idle_s=0).rounds == []


def test_run_by_hand_goes_to_the_upkeep_service(town: Town) -> None:
    from babo.baton import upkeep
    from babo.baton.config import BatonConfig

    assert "starts within a minute" in rounds.request_run(town, "night")
    plan = upkeep.due(town, BatonConfig(town=str(town.root)), now=datetime.now().astimezone(), model_idle_s=0)
    assert ("night", "run by hand") in plan.rounds and plan.run  # now, without waiting for a free model
    with pytest.raises(rounds.RoundError, match="no round called"):
        rounds.request_run(town, "nope")


def test_the_terminal_lists_and_changes_rounds(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console
    from typer.testing import CliRunner

    from babo.baton import cli

    monkeypatch.setattr(cli, "_town", lambda _config: (town, make_config("mayor")))
    monkeypatch.setattr(cli, "console", Console(width=300, force_terminal=False))
    runner = CliRunner()
    listed = runner.invoke(cli.app, ["rounds", "list"])
    assert listed.exit_code == 0 and "The night round" in listed.output and "Part of Babo, always" in listed.output
    changed = runner.invoke(cli.app, ["rounds", "set", "night", "--runs", "now"])
    assert changed.exit_code == 0 and "shares the model with your chats" in changed.output
    assert runner.invoke(cli.app, ["rounds", "set", "night", "--runs", "soon"]).exit_code == 1


def test_residents_cannot_edit_the_towns_rounds(town: Town, make_config) -> None:
    from babo.baton import opencode, profiles

    rules = opencode._permissions(town, "archivist", profiles.Profile(capabilities={"files": "allow"}))  # pyright: ignore[reportPrivateUsage]
    last = [r for r in rules if r["action"] == "edit" and r["resource"] == "rounds/*"]
    assert last and last[-1]["effect"] == "deny"


def test_a_town_from_before_rounds_gets_the_night_round(town: Town) -> None:
    import shutil

    shutil.rmtree(rounds.folder(town))
    assert [r.name for r in rounds.load(town)[0]] == ["night"]
