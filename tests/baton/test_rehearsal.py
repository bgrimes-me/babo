"""Rehearsals and access per step: find out what a round needs, then give it exactly that."""

from __future__ import annotations

import pytest

from babo.baton import rehearsal, rounds
from babo.baton.town import BatonError, Town

ROUND = """---
title: The machine round
when: {daily: '07:00'}
steps:
  - office: archivist
    duty: Check that nano is installed.
  - office: inspector
    duty: Read the report.
    material: previous
---
"""


def _report(town: Town, *, live: bool) -> rehearsal.Rehearsal:
    (rounds.folder(town) / "machine.md").write_text(ROUND)
    r = rehearsal.Rehearsal(id="machine--2026-11-01-180000", round="machine", live=live, status="done", steps=[
        rehearsal.StepReport("archivist", "archivist", "rehearsed", asked=[
            rehearsal.Asked("shell", ["pacman -Q nano"], "allowed" if live else "refused"),
            rehearsal.Asked("webfetch", ["https://archlinux.org"], "refused"),
            rehearsal.Asked("edit", ["../mayor/SELF.md"], "refused")]),
        rehearsal.StepReport("inspector", "inspector", "rehearsed")])
    rehearsal.save(town, r)
    return r


def test_unattended_proposes_what_was_asked_and_live_only_what_the_owner_allowed(town: Town) -> None:
    unattended = _report(town, live=False).proposal()
    assert unattended[0] == {"office": "archivist", "access": {"web_fetch": "allow"}, "commands": ["pacman -Q nano"],
                             "cannot": ["edit ../mayor/SELF.md"]}
    assert _report(town, live=True).proposal()[0] == {"office": "archivist", "access": {}, "commands": ["pacman -Q nano"], "cannot": []}


def test_approving_writes_exactly_that_into_the_round(town: Town) -> None:
    r = _report(town, live=False)
    said = rehearsal.approve(town, r.id)
    assert said == "“The machine round” now has: step 1 (archivist): web_fetch allow, may run `pacman -Q nano`."
    step = rounds.get(town, "machine").steps[0]
    assert step.access == {"web_fetch": "allow"} and step.commands == ("pacman -Q nano",)
    assert rehearsal.latest(town, "machine").approved  # type: ignore[union-attr]


def test_a_step_is_given_access_by_hand_and_nonsense_is_refused(town: Town) -> None:
    (rounds.folder(town) / "machine.md").write_text(ROUND)
    assert rehearsal.set_step_access(town, "machine", 1, {"shell": "allow"}, ["pacman -Q nano"]) == \
        "Step 1 of “The machine round” now: shell allow; may run `pacman -Q nano`."
    assert rehearsal.set_step_access(town, "machine", 1, {"shell": "profile"}, []).endswith("now: as in the profile.")
    with pytest.raises(BatonError, match="isn't something a step can be given"):
        rehearsal.set_step_access(town, "machine", 1, {"sudo": "allow"})
    with pytest.raises(BatonError, match="has 2 step"):
        rehearsal.set_step_access(town, "machine", 3, {"shell": "allow"})
    assert rounds.get(town, "machine").steps[0].access == {}  # nothing half-written


def test_a_round_step_runs_in_its_own_session_with_its_access(town: Town, make_config) -> None:
    import asyncio

    from babo.baton import opencode

    (rounds.folder(town) / "machine.md").write_text(ROUND)
    rehearsal.set_step_access(town, "machine", 1, {"web_fetch": "allow"}, ["pacman -Q nano"])
    session = opencode.special_session(town, make_config("mayor"), "archivist", "round:machine:1",
                                       access={"web_fetch": "allow"}, commands=["pacman -Q nano"])
    assert (session.title, session.overrides, session.commands, session.by) == (
        "archivist: round machine, step 1", {"web_fetch": "allow"}, ["pacman -Q nano"], "owner")
    # Nobody is at a terminal: what would ask is refused; and it waits behind the owner, at upkeep's priority.
    assert opencode._priority("round:machine:1") == "upkeep"  # pyright: ignore[reportPrivateUsage]

    the_town = town

    class Ctx:
        town, work_dir = the_town, the_town.state / "x"
        seen: list[tuple[str, dict[str, str], list[str]]] = []

        async def agent_turn(self, resident: str, message: str, key: str, kind: str = "upkeep",
                             access: dict[str, str] | None = None, commands: list[str] | None = None) -> str:
            self.seen.append((kind, access or {}, commands or []))
            return "ok"

        def log(self, line: str) -> None:
            pass

    ctx = Ctx()
    asyncio.run(rounds.run(ctx, rounds.get(town, "machine"), trigger="t", session_key="k"))
    assert ctx.seen[0] == ("round:machine:1", {"web_fetch": "allow"}, ["pacman -Q nano"])


def test_rehearsals_are_asked_for_and_taken_one_at_a_time(town: Town) -> None:
    (rounds.folder(town) / "machine.md").write_text(ROUND)
    assert "unattended" in rehearsal.request(town, "machine", live=False)
    assert "live" in rehearsal.request(town, "machine", live=True)
    assert [x["live"] for x in (rehearsal.next_request(town), rehearsal.next_request(town))] == [False, True]  # type: ignore[index]
    assert rehearsal.next_request(town) is None


def test_a_rehearsal_puts_the_residents_work_back_but_keeps_its_memory(town: Town) -> None:
    home = town.resident_dir("archivist")
    (home / "notes.md").write_text("before\n")
    snap = rehearsal._snapshot(home)  # pyright: ignore[reportPrivateUsage]
    (home / "notes.md").write_text("changed\n")
    (home / "new.md").write_text("new\n")
    (home / "checkpoints").mkdir(exist_ok=True)
    (home / "checkpoints" / "2026-11-01.jsonl").write_text("{}\n")
    rehearsal._restore(home, snap)  # pyright: ignore[reportPrivateUsage]
    assert (home / "notes.md").read_text() == "before\n" and not (home / "new.md").exists()
    assert (home / "checkpoints" / "2026-11-01.jsonl").exists()
