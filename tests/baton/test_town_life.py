"""Phase 4: visits, crew filing, approved tools, proposals, projects, review, notes, town, bindings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from babo.baton import growth
from babo.baton.inbox import items, unread
from babo.baton.night import Night
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town
from babo.session.manager import SessionPolicy
from baton.mock_llama import MockLlama, malformed, text, tool_call


def _schema_or(chats):
    chats = list(chats)

    def reply(body):
        if "response_format" in body:
            return malformed()
        return chats.pop(0) if chats else text("ok")

    return reply


# -- visits -------------------------------------------------------------------------
async def test_mayor_visits_another_resident(make_loop, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = _schema_or([
        tool_call("visit", {"resident": "archivist", "message": "Is the ledger tidy?"}),
        text("The ledger is tidy, thanks for asking."),  # the archivist's turn
        text("The Archivist says the ledger is tidy."),  # back in the Mayor's turn
    ])
    loop = make_loop("mayor")
    reply = await loop.process_direct("Check on the archivist.", session_key="cli:v")
    assert "ledger is tidy" in str(reply)
    archivist_request = mock_llama.requests[1]
    assert "resident `archivist`" in archivist_request["messages"][0]["content"]
    assert "[visit from mayor] Is the ledger tidy?" in json.dumps(archivist_request["messages"])
    # The visit ran as one turn of the archivist's own loop, logged in its own event log.
    sessions = list((town.resident_dir("archivist") / "sessions").rglob("visit-mayor.events.jsonl"))
    assert sessions
    await loop.aclose()


async def test_only_the_mayor_can_visit_and_never_itself(make_loop) -> None:
    assert "visit" not in make_loop("archivist").tools.tool_names
    loop = make_loop("mayor")
    out = await loop.tools.get("visit").execute(resident="mayor", message="hi")
    assert "can't visit yourself" in out
    out = await loop.tools.get("visit").execute(resident="ghost", message="hi")
    assert "No resident called" in out
    await loop.aclose()


# -- crew -------------------------------------------------------------------------------
async def test_crew_transcripts_are_filed_under_their_resident(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = _schema_or([tool_call("read_note", {"path": "SELF.md"}), text("done")])
    loop = make_loop("mayor")
    await loop.process_direct("hello", session_key="cli:crew")
    await loop.sessions.state.create_child(
        "cli:crew", "subagent:abc123", metadata={}, content="count the beds", policy=SessionPolicy(),
    )
    await loop.aclose()
    mock_llama.default = _schema_or([])
    await Night(make_config("mayor"), night="2026-02-01", prefer_container=False).run()
    filed = list((town.crew / "mayor").rglob("subagent-abc123.jsonl"))
    assert filed and "count the beds" in filed[0].read_text()
    assert not (town.resident_dir("mayor") / "SELF.md").read_text().count("subagent")  # crew have no SELF


# -- tools ---------------------------------------------------------------------------------
def _propose_shout(box, town: Town, *, passing: bool = True) -> None:
    box("mayor").propose_tool(
        "shout", "def run(input: str) -> str:\n    return input.upper()\n",
        "from tool import run\n\ndef test_it():\n    assert run('a') == " + ("'A'" if passing else "'a'") + "\n", "Upper-cases text",
    )
    from babo.baton.tooltest import run_pending_tool_tests

    run_pending_tool_tests(town, timeout_s=30, image="x", prefer_container=False)


async def test_approved_tool_becomes_available_and_runs(box, town: Town, make_loop) -> None:
    _propose_shout(box, town)
    growth.decide_tool(town, "shout", approve=True, reason="useful and safe")
    assert (town.tools / "active" / "shout" / "tool.py").exists()
    assert "shout" in town.ledger.read_text()
    replies = [i for i in items(town, "mayor") if i.kind == "reply"]
    assert replies and "useful and safe" in replies[0].body
    loop = make_loop("inspector")
    assert "shout" in loop.tools.tool_names
    assert await loop.tools.get("shout").execute(input="hello") == "HELLO"
    await loop.aclose()


def test_tool_that_failed_its_test_cannot_be_approved(box, town: Town) -> None:
    _propose_shout(box, town, passing=False)
    with pytest.raises(BatonError, match="not passed"):
        growth.decide_tool(town, "shout", approve=True, reason="x")
    growth.decide_tool(town, "shout", approve=False, reason="the test fails")
    assert (town.tools / "rejected" / "shout").exists()


def test_proposal_accept_and_decline_reach_author(box, town: Town) -> None:
    box("archivist").propose_change("Quiet hours", "Night is busy", "No visits after 22:00")
    box("inspector").propose_change("Longer flags", "More context", "Allow 500 words")
    first, second = growth.open_proposals(town)
    growth.decide_proposal(town, first.path, accept=True, reason="good idea")
    growth.decide_proposal(town, second.path, accept=False, reason="not yet")
    assert len(list((town.proposals / "accepted").glob("*.md"))) == 1
    assert len(list((town.proposals / "declined").glob("*.md"))) == 1
    assert growth.open_proposals(town) == []
    texts = [i.body for r in ("archivist", "inspector") for i in unread(town, r)]
    assert any("good idea" in t for t in texts) and any("not yet" in t for t in texts)


def test_project_promotion_moves_it_to_the_commons(box, town: Town) -> None:
    project = town.resident_dir("mayor") / "projects" / "garden-planner"
    project.mkdir(parents=True)
    (project / "plan.md").write_text("beds: 4\n")
    box("mayor").propose_change("Share the garden planner", "Everyone gardens", "Move it to the commons", project="garden-planner")
    with pytest.raises(BatonError, match="no project"):
        box("mayor").propose_change("x", "y", "z", project="nope")
    prop = growth.open_proposals(town)[0]
    assert prop.project == "garden-planner"
    growth.decide_proposal(town, prop.path, accept=True, reason="yes")
    assert (town.commons_projects / "garden-planner.git").is_dir()
    for resident in ("archivist", "inspector"):
        clone = town.resident_dir(resident) / "projects" / "garden-planner"
        assert (clone / "plan.md").read_text() == "beds: 4\n"
    origin = subprocess.run(["git", "-C", str(project), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    assert origin.endswith("garden-planner.git")
    # The town repo doesn't track project repositories.
    ignored = subprocess.run(["git", "-C", str(town.root), "check-ignore", "commons/projects/garden-planner.git"], capture_output=True)
    assert ignored.returncode == 0


def test_founding_via_review_and_stale_flags(box, town: Town) -> None:
    box("mayor").post_board("founding_request", "Someone to look after the projects.", office="steward")
    request = next(town.board.glob("*founding_request*"))
    new_id = growth.decide_founding(town, request, approve=True, reason="we need help")
    assert new_id == "steward" and Registry(town).get("steward") is not None
    assert any("we need help" in i.body for i in unread(town, "mayor"))
    box("inspector").file_flag("mayor", "claim", "evidence")
    from datetime import datetime, timedelta

    later = datetime.now().astimezone() + timedelta(days=8)
    stale = growth.stale_flags(town, now=later)
    assert len(stale) == 1
    growth.resolve_flag(town, stale[0]["path"], note="talked it through")
    assert growth.stale_flags(town, now=later) == []


# -- CLI: note, town, review ---------------------------------------------------------------
@pytest.fixture
def cli_config(town: Town, index_path: Path, tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"baton": {"town": str(town.root), "indexPath": str(index_path)}, "tools": {"restrictToWorkspace": True}}))
    return path


def test_note_and_town_commands(cli_config: Path, town: Town) -> None:
    from babo.baton.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["note", "archivist", "Thanks for the tidy ledger.", "-c", str(cli_config)])
    assert result.exit_code == 0, result.output
    assert [i.sender for i in unread(town, "archivist")] == ["squid"]
    result = runner.invoke(app, ["town", "-c", str(cli_config)])
    assert result.exit_code == 0 and "archivist" in result.output and "Board" in result.output
    assert runner.invoke(app, ["note", "ghost", "hi", "-c", str(cli_config)]).exit_code == 1


def test_review_walkthrough_with_plain_answers(cli_config: Path, town: Town, box) -> None:
    from babo.baton.cli import app

    _propose_shout(box, town)
    box("mayor").propose_change("Quiet hours", "Night is busy", "No visits after 22:00")
    box("mayor").post_board("founding_request", "A gardener.", office="gardener")
    answers = "\n".join(["a", "safe enough", "d", "maybe later", "s"]) + "\n"
    result = CliRunner().invoke(app, ["review", "-c", str(cli_config)], input=answers)
    assert result.exit_code == 0, result.output
    assert (town.tools / "active" / "shout").exists()
    assert len(list((town.proposals / "declined").glob("*.md"))) == 1
    assert Registry(town).get("gardener") is None  # skipped, not founded
    assert "not an audit" in result.output
    log = subprocess.run(["git", "-C", str(town.root), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("Weekly review: 2 decision(s)")


# -- channel bindings ----------------------------------------------------------------------
def test_channel_bindings_generate_one_config_per_gateway(town: Town, tmp_path: Path) -> None:
    from babo.baton.configs import generate_configs

    base = tmp_path / "config.json"
    base.write_text(json.dumps({
        "gateway": {"port": 18790},
        "channels": {"telegram": {"enabled": True, "token": "${TELEGRAM_TOKEN}"}, "discord": {"enabled": True}, "sendProgress": True},
    }))
    written = generate_configs(base, town, {"telegram": "archivist"}, tmp_path / "gen")
    mayor = json.loads(written["mayor"].read_text())
    arch = json.loads(written["archivist"].read_text())
    assert mayor["channels"]["telegram"]["enabled"] is False and mayor["channels"]["discord"]["enabled"] is True
    assert arch["channels"]["telegram"]["enabled"] is True and arch["channels"]["discord"]["enabled"] is False
    assert arch["channels"]["telegram"]["token"] == "${TELEGRAM_TOKEN}"  # secrets stay references
    assert arch["agents"]["defaults"]["workspace"].endswith("residents/archivist")
    assert {mayor["gateway"]["port"], arch["gateway"]["port"]} == {18790, 18791}
    with pytest.raises(BatonError):
        generate_configs(base, town, {"telegram": "ghost"}, tmp_path / "gen2")


# -- the town's name ------------------------------------------------------------------------
def test_a_resident_proposes_the_town_name_and_accepting_applies_it(box, town: Town) -> None:
    from babo.baton.bundle import build_bundle

    assert town.name == "" and "of this town" in build_bundle(town, "mayor").text()
    box("mayor").propose_change("Name the town", "We should have a name", "Call it Lantern Hill", town_name="Lantern  Hill")
    prop = growth.open_proposals(town)[0]
    assert prop.town_name == "Lantern Hill"
    growth.decide_proposal(town, prop.path, accept=True, reason="lovely")
    assert town.name == "Lantern Hill"
    assert "of Lantern Hill" in build_bundle(town, "archivist").text()
    assert any("now called Lantern Hill" in i.body for i in unread(town, "mayor"))


def test_a_declined_name_changes_nothing(box, town: Town) -> None:
    box("archivist").propose_change("Rename", "why", "what", town_name="Elsewhere")
    growth.decide_proposal(town, growth.open_proposals(town)[0].path, accept=False, reason="not yet")
    assert town.name == ""
    with pytest.raises(BatonError, match="at most 60"):
        box("archivist").propose_change("Rename", "why", "what", town_name="x" * 61)


def test_squid_can_name_the_town_from_the_command_line(cli_config: Path, town: Town) -> None:
    from babo.baton.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["town", "--name", "Lantern Hill", "-c", str(cli_config)])
    assert result.exit_code == 0, result.output
    assert town.name == "Lantern Hill" and "Lantern Hill" in result.output
    log = subprocess.run(["git", "-C", str(town.root), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert "Name the town: Lantern Hill" in log
    assert runner.invoke(app, ["town", "--name", "   ", "-c", str(cli_config)]).exit_code == 1
