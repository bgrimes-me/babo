"""Skills brought in like guides: listed in the index, and brought in when a turn's message (or a tool) calls for one."""

from __future__ import annotations

import json

from babo.baton import guides
from babo.baton.town import Town

SKILL = """---
name: pacman-install
description: Install a package with pacman.
when: installing a package on the machine
keywords: [pacman, install a package]
---
Run `sudo pacman -S --noconfirm <name>`, then check it with `pacman -Q <name>`.
"""


def _skill(town: Town, name: str = "pacman-install", text: str = SKILL) -> None:
    (town.skills / name).mkdir(parents=True, exist_ok=True)
    (town.skills / name / "SKILL.md").write_text(text)


def test_skills_are_listed_in_the_ledger_not_the_index_and_matched_by_keyword(town: Town) -> None:
    from babo.baton.ledger import render_ledger

    before = guides.index_text(town, "mayor")
    _skill(town)
    _skill(town, "plain", "---\nname: plain\ndescription: Something done by hand.\n---\nSteps.\n")
    assert guides.index_text(town, "mayor") == before  # the never-trimmed index doesn't grow with skills
    ledger = render_ledger(town)
    assert "- pacman-install: installing a package on the machine" in ledger and "- plain: Something done by hand." in ledger
    skill = next(g for g in guides.load(town) if g.name == "pacman-install")
    assert skill.kind == "skill" and skill.matches("Please install a package for me") and skill.matches("PACMAN is slow")
    assert not skill.matches("the pacmanager") and not skill.matches("installing packages")
    assert skill.text().startswith("[Skill from Babo: pacman-install.") and "pacman -Q" in skill.text()
    shown = guides.Shown()
    assert [g.name for g in shown.due_for_message("s", guides.load(town), "use pacman")] == ["pacman-install"]
    assert shown.due_for_message("s", guides.load(town), "pacman again") == []  # once a session


def test_a_long_skill_is_cut_short_with_where_to_read_the_rest(town: Town) -> None:
    _skill(town, "long", "---\nname: long\nkeywords: [long]\n---\n" + "x" * 9000)
    text = next(g for g in guides.load(town) if g.name == "long").text()
    assert len(text) < 6300 and 'read_note("skill:long")' in text


def test_opencode_gets_skills_with_keywords_and_opening_one_by_hand_is_logged(town: Town, box) -> None:
    _skill(town)
    data = {g["name"]: g for g in json.loads(guides.plugin_file(town, "archivist"))}
    assert data["pacman-install"]["keywords"] == ["pacman", "install a package"] and data["pacman-install"]["kind"] == "skill"
    assert "pacman -Q" in box("mayor").call("read_note", path="skill:pacman-install")
    [row] = [r for r in guides.stats(town) if r["name"] == "pacman-install"]
    assert (row["kind"], row["opened"]) == ("skill", 1)
    _skill(town, "plain", "---\nname: plain\n---\nSteps.\n")
    [plain] = [r for r in guides.stats(town) if r["name"] == "plain"]
    assert "No triggers" in plain["hint"]


async def test_the_mayor_gets_a_skill_with_the_message_that_calls_for_it_once(make_loop, mock_llama, town: Town) -> None:
    from baton.mock_llama import text

    _skill(town)
    mock_llama.push(text("On it."), text("Done."), text("Sure."))
    loop = make_loop("mayor")
    await loop.process_direct("Can you install a package called nano?", session_key="cli:skills")
    first = [m for m in mock_llama.requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "[Skill from Babo: pacman-install." in json.dumps(first)
    await loop.process_direct("And pacman again for htop?", session_key="cli:skills")
    users = [m for m in mock_llama.requests[-1]["messages"] if m["role"] == "user"]
    assert json.dumps(users).count("[Skill from Babo: pacman-install.") == 1  # in the history once, not again
    await loop.process_direct("Thanks.", session_key="cli:skills")
    from babo.baton.events import resident_events

    assert [e["how"] for e in resident_events(town, "mayor") if e["kind"] == "guide" and e["name"] == "pacman-install"] == ["keyword"]


def test_the_terminal_lists_skills_with_their_keywords(town: Town, make_config, monkeypatch) -> None:
    from rich.console import Console
    from typer.testing import CliRunner

    from babo.baton import cli

    _skill(town)
    monkeypatch.setattr(cli, "_town", lambda _config: (town, make_config("mayor")))
    monkeypatch.setattr(cli, "console", Console(width=300, force_terminal=False))
    result = CliRunner().invoke(cli.app, ["guides"])
    assert result.exit_code == 0 and "pacman-install (skill)" in result.output and "“pacman” in a message" in result.output


def test_the_baton_survives_in_every_shipped_office_however_many_skills_the_town_has(town: Town) -> None:
    """The start of the prompt is never trimmed; the baton is, last. Neither the roles nor the guides' index nor
    the town's skills may leave a resident without its baton in an ordinary session (the default budget)."""
    from babo.baton.baton_file import Checkpoint, append_checkpoint
    from babo.baton.bundle import build_bundle
    from babo.baton.ledger import write_ledger
    from babo.baton.registry import Registry

    for i in range(25):
        _skill(town, f"skill-{i}", f"---\nname: skill-{i}\nwhen: doing thing number {i} for the town\nkeywords: [thing {i}]\n---\nSteps.\n")
    write_ledger(town)
    for resident in ("mayor", "archivist", "inspector"):
        Registry(town).update(resident, first_session_done=True)
        append_checkpoint(town.resident_dir(resident), Checkpoint(thread="the index", doing="listing the notes", next=["write index.md"],
                                                                  resident=resident))
        bundle = build_bundle(town, resident)
        assert "listing the notes" in bundle.now_text(), f"{resident} lost its baton: {bundle.trimmed}"
        assert "baton" not in bundle.trimmed, resident
