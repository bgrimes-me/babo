"""Offices as templates, vacancies, newcomers moving in with their rounds, round proposals, moving away."""

from __future__ import annotations

import pytest

from babo.baton import offices, rounds, views
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town


def test_the_town_ships_the_archivist_and_inspector_offices(town: Town) -> None:
    names = {o.name for o in offices.load(town)[0]}
    assert {"archivist", "inspector"} <= names
    assert "# Role: Archivist" in offices.get(town, "archivist").role  # type: ignore[union-attr]
    Registry(town).update("inspector", status="resting")
    assert "inspector" in [o.name for o in offices.vacancies(town)]


def test_a_newcomer_moves_into_a_new_office_with_its_profile_and_rounds(town: Town) -> None:
    (rounds.folder(town) / "paper.md").write_text(
        "---\ntitle: The paper round\nwhen: {daily: '07:00'}\nsteps:\n  - office: archivist\n    duty: Collect the news.\n---\n")
    said = views.move_in(town, "reporter", role="You write the town's paper, once a day, from what happened.",
                         profile={"web_fetch": "allow", "read": "town"},
                         joins=[{"round": "paper", "duty": "Draft the edition from the news.", "material": "previous"},
                                {"round": "evening", "duty": "Read the edition aloud.", "when": {"daily": "19:00"}}])
    assert said.startswith("reporter moved in as the reporter.") and "joined “paper”" in said and "started the round “evening”" in said
    office = offices.get(town, "reporter")
    assert office is not None and office.profile == {"web_fetch": "allow", "read": "town"}  # the town has the office now
    assert "town's paper" in (town.resident_dir("reporter") / "ROLE.md").read_text()
    paper = rounds.get(town, "paper")
    assert [(s.office, s.material) for s in paper.steps] == [("archivist", "none"), ("reporter", "previous")]
    assert rounds.get(town, "evening").when == {"daily": "19:00"}
    from babo.baton import profiles
    from babo.baton.config import BatonConfig

    mine = profiles.load(town, BatonConfig(), "reporter", "reporter")
    assert mine.level("web_fetch") == "allow" and mine.read == "town"


def test_moving_into_an_office_the_town_has_uses_its_template(town: Town) -> None:
    offices.write(town, offices.Office("gardener", "Gardener", "You tend the projects.", {"shell": "allow"},
                                       (offices.Join("night", "Water the projects."),)))
    said = views.move_in(town, "gardener")
    assert "joined “night”" in said and "You tend the projects." in (town.resident_dir("gardener") / "ROLE.md").read_text()
    assert [s.office for s in rounds.get(town, "night").steps][-1] == "gardener"
    with pytest.raises(BatonError, match="describe its role"):
        views.move_in(town, "baker")
    with pytest.raises(BatonError, match="not the Mayor's"):
        views.move_in(town, "mayor", role="x")


def test_the_mayor_asks_for_a_newcomer_with_its_rounds_and_the_owner_approves_it(town: Town, box) -> None:
    said = box("mayor").call("post_board", kind="move_in_request", text="Someone to keep an eye on the machine.", office="sysadmin",
                             profile={"shell": "ask"}, joins=[{"round": "night", "duty": "Check the disks and the services."}])
    assert "Move-in request posted" in said
    with pytest.raises(BatonError, match="isn't part of a profile"):
        box("mayor").call("post_board", kind="move_in_request", text="x", office="y", profile={"root": "allow"})
    [request] = [p for p in town.board.glob("*sysadmin*.md")]
    assert views.decide(town, "move_in", request.name, yes=True, reason="Good idea.") == "Moved in: sysadmin."
    assert any(s.office == "sysadmin" for s in rounds.get(town, "night").steps)


def test_a_round_is_proposed_read_back_and_written_when_accepted(town: Town, box) -> None:
    mayor = box("mayor")
    assert "The night round" in mayor.call("read_note", path="rounds/night.md")  # residents read how the town works
    assert "Archivist" in mayor.call("read_note", path="offices/archivist.md")
    text = "---\ntitle: The morning round\nwhen: {daily: '08:00'}\nsteps:\n  - office: mayor\n    duty: Read the board.\n---\nEach morning.\n"
    with pytest.raises(BatonError, match="can't be read"):
        mayor.call("propose_change", title="Morning", rationale="r", description="d", round="morning", round_text="---\ntitle: x\n---\n")
    mayor.call("propose_change", title="A morning round", rationale="The board goes unread.", description="Read it each morning.",
               round="morning", round_text=text)
    [proposal] = list((town.proposals / "open").glob("*.md"))
    assert "rounds/morning.md" in proposal.read_text()
    views.decide(town, "proposal", proposal.name, yes=True, reason="Yes.")
    assert rounds.get(town, "morning").when == {"daily": "08:00"}
    mayor.call("propose_change", title="No morning round", rationale="r", description="d", round="morning", remove_round=True)
    [proposal] = list((town.proposals / "open").glob("*.md"))
    views.decide(town, "proposal", proposal.name, yes=True, reason="Fine.")
    assert not (rounds.folder(town) / "morning.md").exists()


def test_a_resident_moves_away_and_its_office_is_a_vacancy_again(town: Town) -> None:
    said = views.move_away(town, "inspector")
    assert "inspector has moved away" in said and Registry(town).get("inspector").status == "moved_away"  # type: ignore[union-attr]
    assert town.resident_dir("inspector").exists()  # its folder and memory stay
    assert "inspector" in [o.name for o in offices.vacancies(town)]
    assert views.move_in(town, "inspector").startswith("inspector-2 moved in")  # a newcomer, not the one who left
    assert offices.holders(town, "inspector") == ["inspector-2"] and rounds.holder(town, "inspector") == "inspector-2"
    with pytest.raises(BatonError, match="Mayor can't move away"):
        views.move_away(town, "mayor")


def test_residents_cannot_edit_the_towns_offices(town: Town) -> None:
    from babo.baton import opencode, profiles

    rules = opencode._permissions(town, "archivist", profiles.Profile(capabilities={"files": "allow"}))  # pyright: ignore[reportPrivateUsage]
    assert [r for r in rules if r["action"] == "edit" and r["resource"] == "offices/*"][-1]["effect"] == "deny"


def test_the_terminal_moves_in_lists_offices_and_moves_away(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console
    from typer.testing import CliRunner

    from babo.baton import cli

    monkeypatch.setattr(cli, "_town", lambda _config: (town, make_config("mayor")))
    monkeypatch.setattr(cli, "console", Console(width=300, force_terminal=False))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["move-in", "reporter", "--role", "You write the paper.", "--set", "web_fetch=allow",
                                     "--join", "night: Write the morning edition."])
    assert result.exit_code == 0, result.output
    assert "reporter moved in" in result.output and "joined “night”" in result.output
    assert runner.invoke(cli.app, ["move-in", "baker", "--join", "night"]).exit_code != 0
    listed = runner.invoke(cli.app, ["offices"]).output
    assert "Reporter" in listed and "web_fetch allow" in listed and "night: Write the morning edition." in listed
    assert runner.invoke(cli.app, ["move-away", "reporter"], input="n\n").exit_code == 0
    assert Registry(town).get("reporter").status == "active"  # type: ignore[union-attr]  # said no
    result = runner.invoke(cli.app, ["move-away", "reporter", "--yes"])
    assert result.exit_code == 0 and "moved away" in result.output
    assert "vacancy" in runner.invoke(cli.app, ["offices"]).output
